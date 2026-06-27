"""Best-effort append-only security audit logging."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from typing import Any, Optional

from fastapi import Request

from app.config import settings

logger = logging.getLogger(__name__)


def _audit_key() -> bytes:
    secret = settings.jwt_secret_key.strip() or settings.admin_secret_token.strip()
    if not secret:
        logger.warning(
            "audit_key: neither JWT_SECRET_KEY nor ADMIN_SECRET_TOKEN is set; "
            "audit HMAC will use a development fallback that is not persistent "
            "across restarts. Set at least one secret for production."
        )
        secret = "development-audit-key"
    return secret.encode("utf-8")


def _request_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    # Only trust X-Forwarded-For when the deployment is configured to
    # sit behind a trusted reverse proxy (matching limiter.py behavior).
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip() or None
    return request.client.host if request.client else None


def _canonical_metadata(metadata: Optional[dict[str, Any]]) -> str:
    if not metadata:
        return "{}"
    return json.dumps(metadata, sort_keys=True, separators=(",", ":"), default=str)


def _build_digest(
    *,
    event_type: str,
    actor_user_id: Optional[int],
    actor_username: Optional[str],
    target_user_id: Optional[int],
    target_username: Optional[str],
    ip_address: Optional[str],
    user_agent: Optional[str],
    metadata_json: str,
    key_version: str,
) -> str:
    message = "|".join(
        [
            event_type,
            str(actor_user_id or ""),
            actor_username or "",
            str(target_user_id or ""),
            target_username or "",
            ip_address or "",
            user_agent or "",
            metadata_json,
            key_version,
        ]
    )
    return hmac.new(_audit_key(), message.encode("utf-8"), hashlib.sha256).hexdigest()


def record_security_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor_user_id: Optional[int] = None,
    actor_username: Optional[str] = None,
    target_user_id: Optional[int] = None,
    target_username: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Insert one security audit event and commit it."""
    metadata_json = _canonical_metadata(metadata)
    key_version = settings.audit_hmac_key_version
    digest = _build_digest(
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        target_user_id=target_user_id,
        target_username=target_username,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata_json=metadata_json,
        key_version=key_version,
    )
    conn.execute(
        """
        INSERT INTO security_audit_log(
            event_type,
            actor_user_id,
            actor_username,
            target_user_id,
            target_username,
            ip_address,
            user_agent,
            metadata_json,
            key_version,
            hmac_sha256
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            actor_user_id,
            actor_username,
            target_user_id,
            target_username,
            ip_address,
            user_agent,
            metadata_json,
            key_version,
            digest,
        ),
    )
    conn.commit()


async def safe_record_security_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor: Optional[dict[str, Any]] = None,
    target_user_id: Optional[int] = None,
    target_username: Optional[str] = None,
    request: Optional[Request] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Best-effort async wrapper for request handlers."""
    try:
        await asyncio.to_thread(
            record_security_event,
            conn,
            event_type=event_type,
            actor_user_id=actor.get("id") if actor else None,
            actor_username=actor.get("username") if actor else None,
            target_user_id=target_user_id,
            target_username=target_username,
            ip_address=_request_ip(request),
            user_agent=request.headers.get("user-agent") if request else None,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Security audit logging failed for %s: %s", event_type, exc)
