"""Admin routes for managing feature toggles."""

import asyncio
import hashlib
import hmac
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import (
    get_db,
    get_maintenance_service,
    get_secret_manager,
    get_toggle_manager,
)
from app.config import settings
from app.security import csrf_protect, require_scope
from app.services.maintenance import MaintenanceService
from app.services.secret_manager import SecretManager
from app.services.toggle_manager import ToggleManager

router = APIRouter(prefix="/admin", tags=["admin"])


class TogglePayload(BaseModel):
    feature: str
    enabled: bool


class MaintenancePayload(BaseModel):
    enabled: bool
    reason: str = ""


class MaintenanceResponse(BaseModel):
    enabled: bool
    reason: str
    version: int
    updated_at: str | None


def _compute_hmac(key: bytes, feature: str, enabled: bool, user_id: str | None, ip: str | None) -> tuple[str, str]:
    timestamp = datetime.now(timezone.utc).isoformat()
    message = f"{feature}|{int(enabled)}|{user_id or ''}|{ip or ''}|{timestamp}"
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest, timestamp


def _maintenance_response(service: MaintenanceService) -> MaintenanceResponse:
    flag = service.get_flag()
    return MaintenanceResponse(
        enabled=flag.enabled,
        reason=flag.reason,
        version=flag.version,
        updated_at=flag.updated_at,
    )


def _write_toggle_with_audit(
    conn: sqlite3.Connection,
    toggle_manager: ToggleManager,
    feature: str,
    enabled: bool,
    user_id: str | None,
    ip: str | None,
    key_version: str,
    hmac_digest: str,
    timestamp: str,
) -> None:
    if conn.in_transaction:
        conn.rollback()
    try:
        conn.execute("BEGIN IMMEDIATE")
        toggle_manager.set_toggle_on_connection(conn, feature, enabled)
        conn.execute(
            """
            INSERT INTO audit_toggle_log(feature, enabled, user_id, ip, timestamp, key_version, hmac_sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feature,
                int(enabled),
                user_id,
                ip,
                timestamp,
                key_version,
                hmac_digest,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    toggle_manager.update_cache(feature, enabled)


@router.post("/toggles")
async def set_toggle(
    payload: TogglePayload,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    toggle_manager: ToggleManager = Depends(get_toggle_manager),
    secret_manager: SecretManager = Depends(get_secret_manager),
    auth: dict = Depends(require_scope("admin:config")),
):
    key, key_version = secret_manager.get_hmac_key()
    try:
        hmac_digest, timestamp = _compute_hmac(
            key,
            payload.feature,
            payload.enabled,
            auth.get("user_id"),
            request.client.host if request.client else None,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to compute audit HMAC: {exc}")
    try:
        await asyncio.to_thread(
            _write_toggle_with_audit,
            conn,
            toggle_manager,
            payload.feature,
            payload.enabled,
            auth.get("user_id"),
            request.client.host if request.client else None,
            key_version,
            hmac_digest,
            timestamp,
        )
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"Failed to update toggle: {exc}")
    request.app.state.model_validation = await asyncio.to_thread(
        toggle_manager.get_toggle, "model_validation", settings.enable_model_validation
    )
    return {"feature": payload.feature, "enabled": payload.enabled, "hmac": hmac_digest}


@router.get("/maintenance", response_model=MaintenanceResponse)
async def get_maintenance(
    service: MaintenanceService = Depends(get_maintenance_service),
    _auth: dict = Depends(require_scope("admin:config")),
) -> MaintenanceResponse:
    """Return current write-blocking maintenance flag state."""
    return await asyncio.to_thread(_maintenance_response, service)


@router.post("/maintenance", response_model=MaintenanceResponse)
async def set_maintenance(
    payload: MaintenancePayload,
    service: MaintenanceService = Depends(get_maintenance_service),
    _auth: dict = Depends(require_scope("admin:config")),
    _csrf_token: str = Depends(csrf_protect),
) -> MaintenanceResponse:
    """Set write-blocking maintenance flag state."""
    await asyncio.to_thread(service.set_flag, payload.enabled, payload.reason)
    return await asyncio.to_thread(_maintenance_response, service)
