"""Admin routes for managing feature toggles."""

import asyncio
import hashlib
import hmac
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import get_db, get_secret_manager, get_toggle_manager
from app.config import settings
from app.security import require_scope
from app.services.secret_manager import SecretManager
from app.services.toggle_manager import ToggleManager

router = APIRouter(prefix="/admin", tags=["admin"])


class TogglePayload(BaseModel):
    feature: str
    enabled: bool


def _compute_hmac(key: bytes, feature: str, enabled: bool, user_id: str | None, ip: str | None) -> tuple[str, str]:
    timestamp = datetime.now(timezone.utc).isoformat()
    message = f"{feature}|{int(enabled)}|{user_id or ''}|{ip or ''}|{timestamp}"
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest, timestamp


@router.post("/toggles")
async def set_toggle(
    payload: TogglePayload,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    toggle_manager: ToggleManager = Depends(get_toggle_manager),
    secret_manager: SecretManager = Depends(get_secret_manager),
    auth: dict = Depends(require_scope("admin:config")),
):
    await asyncio.to_thread(toggle_manager.set_toggle, payload.feature, payload.enabled)
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
    await asyncio.to_thread(
        conn.execute,
        """
        INSERT INTO audit_toggle_log(feature, enabled, user_id, ip, timestamp, key_version, hmac_sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.feature,
            int(payload.enabled),
            auth.get("user_id"),
            request.client.host if request.client else None,
            timestamp,
            key_version,
            hmac_digest,
        ),
    )
    await asyncio.to_thread(conn.commit)
    request.app.state.model_validation = await asyncio.to_thread(
        toggle_manager.get_toggle, "model_validation", settings.enable_model_validation
    )
    return {"feature": payload.feature, "enabled": payload.enabled, "hmac": hmac_digest}
