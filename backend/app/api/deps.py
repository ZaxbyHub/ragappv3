"""FastAPI dependency functions."""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable
from enum import IntEnum
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import Cookie, Depends, Header, HTTPException, Request

from app.config import Settings, settings
from app.models.database import SQLiteConnectionPool, get_pool
from app.security import get_csrf_manager  # noqa: F401
from app.services.auth_service import (
    TokenExpiredError,
    TokenInvalidError,
    decode_access_token,
)

logger = logging.getLogger(__name__)

# SQLite thread-safety guard for parallelized vault permission queries.
# asyncio.to_thread runs callables in separate OS threads; sharing a single
# sqlite3.Connection across those threads is only safe when SQLite is built
# with SERIALIZED threading (sqlite3.threadsafety == 3). Otherwise, fall back
# to sequential to_thread calls to avoid "SQLite objects created in a thread
# can only be used in that same thread" and subtle data corruption.
_SQLITE_SERIALIZED = sqlite3.threadsafety == 3
_FALLBACK_WARNED = False


def _warn_fallback_threading() -> None:
    """Log the sequential fallback warning once per process."""
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    logger.warning(
        "SQLite threading mode is %s; get_effective_vault_permissions will "
        "run sequentially. For parallel execution, use a SERIALIZED build "
        "(sqlite3.threadsafety == 3).",
        sqlite3.threadsafety,
    )


class UserRole(IntEnum):
    """Canonical role hierarchy used for all authorization checks."""

    VIEWER = 1
    MEMBER = 2
    ADMIN = 3
    SUPERADMIN = 4

    @classmethod
    def level(cls, role_name: str) -> int:
        """Get numeric level for a role string, defaulting to 0 for unknown roles."""
        try:
            return cls[role_name.upper()].value
        except (KeyError, AttributeError):
            return 0


VAULT_PERMISSION_LEVELS = {"read": 1, "write": 2, "admin": 3}
VAULT_PERMISSION_NAMES = {0: None, 1: "read", 2: "write", 3: "admin"}
VAULT_ACTION_LEVELS = {"read": 1, "write": 2, "delete": 3, "admin": 3}

_ACTIVE_USER_CACHE: dict[str, tuple[dict, float]] = {}
_ACTIVE_USER_CACHE_LOCK = Lock()
_ACTIVE_USER_CACHE_KEY_FORMAT = "active_user:{user_id}"


def _build_active_user_cache_key(user_id: int) -> str:
    return _ACTIVE_USER_CACHE_KEY_FORMAT.format(user_id=user_id)


def invalidate_active_user_cache(user_id: int) -> None:
    """Remove cached active-user state for ``user_id``.

    Mutation endpoints should call this after successful writes so
    subsequent requests see the updated row. Multiple IDs can be
    invalidated by calling this function in a loop.
    """
    cache_key = _build_active_user_cache_key(user_id)
    with _ACTIVE_USER_CACHE_LOCK:
        _ACTIVE_USER_CACHE.pop(cache_key, None)



# Lazy imports — services are only loaded when their getter is actually called.
# This prevents heavy imports (unstructured, aioimaplib, torch, etc.) from
# blocking every request handler import chain.
if TYPE_CHECKING:
    from app.services.background_tasks import BackgroundProcessor
    from app.services.email_service import EmailIngestionService
    from app.services.embeddings import EmbeddingService
    from app.services.llm_client import LLMClient
    from app.services.llm_health import LLMHealthChecker
    from app.services.maintenance import MaintenanceService
    from app.services.memory_store import MemoryStore
    from app.services.model_checker import ModelChecker
    from app.services.rag_engine import RAGEngine
    from app.services.secret_manager import SecretManager
    from app.services.toggle_manager import ToggleManager
    from app.services.vector_store import VectorStore


def get_db():
    """Yield a database connection from the pool, releasing it when done."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        yield conn
    finally:
        pool.release_connection(conn)


def get_db_pool(request: Request) -> SQLiteConnectionPool:
    """Return the database pool from app state."""
    return request.app.state.db_pool


def get_settings() -> Settings:
    """Return the application settings."""
    return settings


def get_llm_client(request: Request) -> LLMClient:
    """Return the LLM client from app state."""
    return request.app.state.llm_client


def get_embedding_service(request: Request) -> EmbeddingService:
    """Return the embedding service from app state."""
    return request.app.state.embedding_service


def get_vector_store(request: Request) -> VectorStore:
    """Return the vector store from app state."""
    return request.app.state.vector_store


def get_memory_store(request: Request) -> MemoryStore:
    """Return the memory store from app state."""
    return request.app.state.memory_store


def get_reranking_service(request: Request):
    """Return the RerankingService from app state."""
    return request.app.state.reranking_service


def get_rag_engine(request: Request) -> RAGEngine:
    """Return the cached RAGEngine singleton from app state."""
    return request.app.state.rag_engine


def get_toggle_manager(request: Request) -> ToggleManager:
    """Return the toggle manager from app state."""
    return request.app.state.toggle_manager


def get_secret_manager(request: Request) -> SecretManager:
    """Return the secret manager from app state."""
    return request.app.state.secret_manager


def get_background_processor(request: Request) -> BackgroundProcessor:
    """Return the background processor from app state."""
    return request.app.state.background_processor


def get_maintenance_service(request: Request) -> MaintenanceService:
    """Return the maintenance service from app state."""
    return request.app.state.maintenance_service


def get_llm_health_checker(request: Request) -> LLMHealthChecker:
    """Return the LLM health checker from app state."""
    return request.app.state.llm_health_checker


def get_model_checker(request: Request) -> ModelChecker:
    """Return the model checker from app state."""
    return request.app.state.model_checker


def get_email_service(request: Request) -> EmailIngestionService:
    """Return the email ingestion service from app state."""
    return request.app.state.email_service


async def get_current_active_user(
    request: Request,
    authorization: str | None = Header(None),
    access_token: str | None = Cookie(None),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    FastAPI dependency to get the current authenticated user.

    When users_enabled=False: Validates against admin_secret_token
    When users_enabled=True: Validates JWT token and fetches user from database
    """
    # Extract token from Authorization header or fall back to cookie
    if authorization and authorization.lower().startswith("bearer "):
        parts = authorization.split(" ", 1)
        if len(parts) >= 2 and parts[1].strip():
            token = parts[1].strip()
        else:
            token = access_token
    else:
        token = access_token

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # When users are disabled, fall back to admin token authentication
    if not settings.users_enabled:
        DEFAULT_TOKEN = "admin-secret-token"

        if not settings.admin_secret_token and secrets.compare_digest(
            token, DEFAULT_TOKEN
        ):
            raise HTTPException(
                status_code=403,
                detail="Invalid credentials - change default admin token",
            )

        if not secrets.compare_digest(token, settings.admin_secret_token):
            raise HTTPException(status_code=403, detail="Invalid credentials")

        return {
            "id": 0,
            "username": "admin",
            "full_name": "Admin",
            "role": "superadmin",
            "is_active": True,
            "must_change_password": False,
        }

    # User authentication enabled - validate JWT token
    try:
        payload = decode_access_token(token)
    except TokenExpiredError:
        raise HTTPException(
            status_code=401,
            detail="token_expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except TokenInvalidError:
        raise HTTPException(
            status_code=401,
            detail="token_invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Enforce token type — reject refresh tokens used as access tokens
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=401,
            detail="token_invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = int(payload.get("sub", 0))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=401,
            detail="token_invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="token_invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )

    cache_key = _build_active_user_cache_key(user_id)
    ttl = settings.active_user_cache_ttl_seconds
    cached_user = None

    if ttl > 0:
        with _ACTIVE_USER_CACHE_LOCK:
            cached_entry = _ACTIVE_USER_CACHE.get(cache_key)
        if cached_entry is not None:
            _cached_user, expires_at = cached_entry
            if expires_at > time.monotonic():
                cached_user = _cached_user
                user = cached_user
            else:
                with _ACTIVE_USER_CACHE_LOCK:
                    _ACTIVE_USER_CACHE.pop(cache_key, None)

    if cached_user is None:
        row = await asyncio.to_thread(
            lambda: db.execute(
                "SELECT id, username, full_name, role, is_active, must_change_password FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        )

        if not row:
            raise HTTPException(
                status_code=401,
                detail="token_invalid",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user = {
            "id": row[0],
            "username": row[1],
            "full_name": row[2] or "",
            "role": row[3],
            "is_active": bool(row[4]),
            "must_change_password": bool(row[5])
            if len(row) > 5 and row[5] is not None
            else False,
        }

        if ttl > 0:
            with _ACTIVE_USER_CACHE_LOCK:
                _ACTIVE_USER_CACHE[cache_key] = (user, time.monotonic() + ttl)
    else:
        user = cached_user

    if not user["is_active"]:
        raise HTTPException(
            status_code=401,
            detail="user_inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Enforce must_change_password: flagged users can only access explicit
    # auth recovery routes. Include both router-local and mounted runtime paths.
    if user.get("must_change_password"):
        # Base paths always included (no prefix)
        # Note: request.url.path does NOT include root_path (FastAPI strips it before routing),
        # so prefixed paths like /meridian/api/... are never seen here — only /api/...
        exempt_paths = {
            "/auth/change-password",
            "/auth/login",
            "/api/auth/change-password",
            "/api/auth/login",
        }
        # GET /auth/me is exempt so the SPA can rehydrate the current user and
        # detect the flag on reload — it is a read-only view of the caller's own
        # identity (no other data, no mutation). Without it, the client's init()
        # fetchMe() 403s, the user is logged out, and is bounced to login instead
        # of the forced change-password screen. Only GET is exempt: PATCH /auth/me
        # (update_me) mutates the profile/password and must stay blocked so the
        # forced-change flow is the only way out.
        me_read_paths = {"/auth/me", "/api/auth/me"}
        # Normalize path to prevent bypass via trailing slash variants
        normalized_path = request.url.path.rstrip("/") or "/"
        is_exempt = normalized_path in exempt_paths or (
            request.method.upper() == "GET" and normalized_path in me_read_paths
        )
        if not is_exempt:
            raise HTTPException(
                status_code=403,
                detail="must_change_password",
            )

    return user


async def get_effective_vault_permission(
    db: sqlite3.Connection,
    principal: dict,
    vault_id: int | None,
) -> str | None:
    """Return the user's strongest effective permission on a vault."""
    if vault_id is None:
        return None
    result = await get_effective_vault_permissions(db, principal, [vault_id])
    return result.get(vault_id)


async def get_effective_vault_permissions(
    db: sqlite3.Connection,
    principal: dict,
    vault_ids: list[int],
) -> dict[int, str | None]:
    """Return each vault's strongest effective permission for the user."""
    user_id = principal.get("id")
    user_role = principal.get("role", "")
    normalized_ids = list(dict.fromkeys(vault_ids))

    if user_id is None or not normalized_ids:
        return {}

    if user_role == "superadmin":
        return {vault_id: "admin" for vault_id in normalized_ids}

    baseline_level = VAULT_PERMISSION_LEVELS["write"] if user_role == "admin" else 0
    effective_levels = {vault_id: baseline_level for vault_id in normalized_ids}
    placeholders = ",".join("?" for _ in normalized_ids)

    def _query_vault_members():
        return db.execute(
            f"""SELECT vault_id, permission FROM vault_members
                WHERE user_id = ? AND vault_id IN ({placeholders})""",
            (user_id, *normalized_ids),
        ).fetchall()

    def _query_group_access():
        return db.execute(
            f"""SELECT vga.vault_id, vga.permission FROM vault_group_access vga
               JOIN group_members gm ON vga.group_id = gm.group_id
               WHERE gm.user_id = ? AND vga.vault_id IN ({placeholders})""",
            (user_id, *normalized_ids),
        ).fetchall()

    def _query_public_vaults():
        return db.execute(
            f"""SELECT v.id FROM vaults v
                WHERE v.visibility = 'public' AND v.id IN ({placeholders})
                AND (v.org_id IS NULL OR EXISTS (
                    SELECT 1 FROM org_members WHERE org_id = v.org_id AND user_id = ?
                ))""",
            (*normalized_ids, user_id),
        ).fetchall()

    def _query_org_vaults():
        return db.execute(
            f"""SELECT v.id FROM vaults v
                WHERE v.visibility = 'org' AND v.id IN ({placeholders})
                AND v.org_id IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM org_members WHERE org_id = v.org_id AND user_id = ?
                )""",
            (*normalized_ids, user_id),
        ).fetchall()

    if _SQLITE_SERIALIZED:
        members_rows, group_rows, public_rows, org_rows = await asyncio.gather(
            asyncio.to_thread(_query_vault_members),
            asyncio.to_thread(_query_group_access),
            asyncio.to_thread(_query_public_vaults),
            asyncio.to_thread(_query_org_vaults),
        )
    else:
        _warn_fallback_threading()
        members_rows = await asyncio.to_thread(_query_vault_members)
        group_rows = await asyncio.to_thread(_query_group_access)
        public_rows = await asyncio.to_thread(_query_public_vaults)
        org_rows = await asyncio.to_thread(_query_org_vaults)

    for vault_id, permission in members_rows:
        effective_levels[vault_id] = max(
            effective_levels[vault_id],
            VAULT_PERMISSION_LEVELS.get(permission, 0),
        )

    for vault_id, permission in group_rows:
        effective_levels[vault_id] = max(
            effective_levels[vault_id],
            VAULT_PERMISSION_LEVELS.get(permission, 0),
        )

    for (vault_id,) in public_rows:
        effective_levels[vault_id] = max(
            effective_levels[vault_id],
            VAULT_PERMISSION_LEVELS["read"],
        )

    for (vault_id,) in org_rows:
        effective_levels[vault_id] = max(
            effective_levels[vault_id],
            VAULT_PERMISSION_LEVELS["read"],
        )

    return {
        vault_id: VAULT_PERMISSION_NAMES.get(effective_levels[vault_id])
        for vault_id in normalized_ids
    }


async def _evaluate_policy(
    db: sqlite3.Connection,
    principal: dict,
    resource_type: str,
    resource_id: int | None,
    action: str,
) -> bool:
    """Core policy evaluation logic with injected database connection."""
    user_id = principal.get("id")
    user_role = principal.get("role", "")

    if user_id is None:
        return False

    if resource_type not in ("vault", "group"):
        return user_role == "superadmin"

    # Group resources: admin and superadmin have full access
    if resource_type == "group":
        return user_role in ("superadmin", "admin")

    if resource_id is None:
        return False

    if user_role == "superadmin":
        return True

    effective_permission = await get_effective_vault_permission(db, principal, resource_id)
    effective_level = VAULT_PERMISSION_LEVELS.get(effective_permission or "", 0)
    required_level = VAULT_ACTION_LEVELS.get(action, VAULT_ACTION_LEVELS["read"])
    return effective_level >= required_level


def get_evaluate_policy(
    db: sqlite3.Connection = Depends(get_db),
) -> Callable:
    """FastAPI dependency that returns an evaluate function with injected database.

    Usage:
        evaluate = Depends(get_evaluate_policy)
        if await evaluate(user, "vault", vault_id, "read"):
            # permission granted
    """

    async def _evaluate(
        principal: dict,
        resource_type: str,
        resource_id: int | None,
        action: str,
    ) -> bool:
        return await _evaluate_policy(db, principal, resource_type, resource_id, action)

    return _evaluate


async def evaluate_policy(
    principal: dict,
    resource_type: str,
    resource_id: int | None,
    action: str,
) -> bool:
    """
    Original evaluate_policy - creates pool for backward compatibility.

    Resolution order (vault resources):
    1. superadmin -> True for all actions
    2. admin -> baseline level from admin role (read/write), then membership and group permissions layered on top
    3. vault_members row -> use permission column (highest of baseline and explicit)
    4. vault_group_access (user in group) -> highest permission wins
    5. vault.visibility == 'public' -> True (org-scoped for vaults with org_id)
    6. vault.visibility == 'org' -> True only if user is a member of the vault's org
    7. Otherwise -> False
    """
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        return await _evaluate_policy(
            conn, principal, resource_type, resource_id, action
        )
    finally:
        pool.release_connection(conn)


def require_vault_permission(*actions: str):
    """
    FastAPI dependency for vault permission checks.

    Creates a dependency that validates the current user has at least
    one of the specified permissions on the given vault.

    Usage: Depends(require_vault_permission("read", "admin"))
    """

    async def _check(
        vault_id: int,
        user: dict = Depends(get_current_active_user),
        db: sqlite3.Connection = Depends(get_db),
    ):
        evaluate = get_evaluate_policy(db)
        for action in actions:
            if await evaluate(user, "vault", vault_id, action):
                return user
        raise HTTPException(status_code=403, detail="Insufficient vault permissions")

    return _check


def require_role(role: str):
    """
    FastAPI dependency to require a specific role or higher.

    Uses UserRole enum for canonical hierarchy:
    superadmin(4) > admin(3) > member(2) > viewer(1)

    Usage: Depends(require_role("admin"))
    """
    required_level = UserRole.level(role)

    async def _check_role(user: dict = Depends(get_current_active_user)) -> dict:
        user_role = user.get("role", "viewer")
        user_level = UserRole.level(user_role)

        if user_level < required_level:
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient privileges. Required role: {role}",
            )

        return user

    return _check_role


async def require_admin_role(user: dict = Depends(get_current_active_user)) -> dict:
    """
    Dependency that requires the user to have admin or superadmin role.
    Uses UserRole enum for canonical hierarchy check.
    """
    user_role = user.get("role", "")
    if UserRole.level(user_role) < UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Admin access required",
        )
    return user


async def get_user_accessible_vault_ids(user: dict, db) -> list:
    """
    Get all vault IDs that a user has access to.

    Returns list of vault IDs based on:
    - Direct vault_members permissions
    - vault_group_access via group membership
    - For superadmin/admin: returns empty list (means "all vaults")
    """
    user_role = user.get("role", "")

    # superadmin/admin can access all vaults
    if user_role in ("superadmin", "admin"):
        return []  # Empty list means "all vaults"

    rows = await asyncio.to_thread(lambda: db.execute("SELECT id FROM vaults").fetchall())
    vault_ids = [row[0] for row in rows]
    permissions = await get_effective_vault_permissions(db, user, vault_ids)
    return [
        vault_id
        for vault_id, permission in permissions.items()
        if permission is not None
    ]


class MultipleOrgError(Exception):
    """Raised when a user belongs to multiple organizations."""

    pass


async def get_user_orgs(user_id: int, db: sqlite3.Connection) -> list[int]:
    """Get all organization IDs for a user.

    Args:
        user_id: The user's ID
        db: Database connection

    Returns:
        List of organization IDs the user belongs to
    """
    rows = await asyncio.to_thread(
        lambda: db.execute(
            "SELECT org_id FROM org_members WHERE user_id = ?", (user_id,)
        ).fetchall()
    )
    return [row[0] for row in rows]


async def get_user_primary_org(user_id: int, db: sqlite3.Connection) -> int | None:
    """Get the primary organization ID for a user.

    Args:
        user_id: The user's ID
        db: Database connection

    Returns:
        The organization ID if user belongs to exactly one org
        None if user belongs to no orgs

    Raises:
        MultipleOrgError: If user belongs to multiple organizations
    """
    orgs = await get_user_orgs(user_id, db)
    if len(orgs) == 0:
        return None
    if len(orgs) == 1:
        return orgs[0]
    raise MultipleOrgError(f"User {user_id} belongs to multiple organizations: {orgs}")


def assign_user_to_default_vault(db: sqlite3.Connection, user_id: int) -> None:
    """
    Assign a user to the Default vault with read permission.

    Idempotent — uses INSERT OR IGNORE. Creates the Default vault if it
    doesn't exist. Gracefully handles pre-migration state when the vaults
    or vault_members tables don't exist yet.
    """
    try:
        cursor = db.execute("SELECT id FROM vaults WHERE name = ?", ("Default",))
        row = cursor.fetchone()
        if row:
            vault_id = row[0]
        else:
            cursor = db.execute(
                "INSERT OR IGNORE INTO vaults (name, description) VALUES (?, ?)",
                ("Default", "Default vault for all users"),
            )
            vault_id = cursor.lastrowid or db.execute(
                "SELECT id FROM vaults WHERE name = ?", ("Default",)
            ).fetchone()[0]
        db.execute(
            "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (vault_id, user_id, "read"),
        )
    except sqlite3.OperationalError:
        # vaults or vault_members table doesn't exist yet (pre-migration)
        logger.warning("Could not assign user %d to Default vault (tables may not exist yet)", user_id)
