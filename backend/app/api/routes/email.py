"""
Email ingestion status API route.

Provides admin-only endpoints to check email ingestion service status,
including health, statistics, and IMAP inbox status.
"""

import asyncio
from typing import Optional

import aioimaplib
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_email_service, get_settings
from app.config import Settings, settings
from app.security import require_scope
from app.services.email_service import EmailIngestionService

router = APIRouter()


class EmailStatusResponse(BaseModel):
    """Response schema for email ingestion status endpoint."""
    enabled: bool
    healthy: bool
    last_poll: Optional[str]  # ISO timestamp or None
    emails_processed_today: int
    emails_failed_today: int
    unseen_emails: int
    current_backoff_delay: Optional[int]  # seconds or None if connected


async def _get_unseen_count(
    email_service: EmailIngestionService
) -> int:
    """
    Query IMAP for unseen email count.

    Args:
        email_service: EmailIngestionService instance with IMAP settings

    Returns:
        Number of unseen emails, or -1 if connection fails
    """
    if not email_service.settings.imap_enabled:
        return 0

    imap_client = None
    try:
        # Connect with or without SSL based on settings
        if email_service.settings.imap_use_ssl:
            imap_client = aioimaplib.IMAP4_SSL(
                host=email_service.settings.imap_host,
                port=email_service.settings.imap_port,
                timeout=30
            )
        else:
            imap_client = aioimaplib.IMAP4(
                host=email_service.settings.imap_host,
                port=email_service.settings.imap_port,
                timeout=30
            )

        # Authenticate
        result = await imap_client.wait_hello_from_server()
        if result != 'OK':
            return -1

        result, _ = await imap_client.login(
            email_service.settings.imap_username,
            email_service.settings.imap_password.get_secret_value()
        )
        if result != 'OK':
            return -1

        # Select mailbox
        result = await imap_client.select(email_service.settings.imap_mailbox)
        if result != 'OK':
            return -1

        # Search for UNSEEN emails
        result, data = await imap_client.search(None, 'UNSEEN')
        if result != 'OK':
            return -1

        # Count UIDs
        uids = data[0].split()
        return len(uids)

    except (asyncio.TimeoutError, OSError, ConnectionError) as e:
        # Log but don't fail the entire status endpoint
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to query IMAP for unseen count: {e}")
        return -1
    finally:
        if imap_client:
            try:
                await imap_client.logout()
            except (asyncio.TimeoutError, OSError, ConnectionError):
                # Ignore logout errors
                pass


def _get_today_stats(pool) -> tuple[int, int]:
    """
    Query database for today's email processing stats.

    Args:
        pool: SQLiteConnectionPool instance

    Returns:
        Tuple of (processed_today, failed_today) counts
    """
    from datetime import date

    conn = pool.get_connection()
    try:
        today = date.today().isoformat()

        # Get processed count (all emails added today)
        cursor = conn.execute(
            """
            SELECT COUNT(*) as count
            FROM files
            WHERE source = 'email'
            AND DATE(created_at) = ?
            """,
            (today,)
        )
        processed_today = cursor.fetchone()["count"]

        # Get failed count (emails with error status today)
        cursor = conn.execute(
            """
            SELECT COUNT(*) as count
            FROM files
            WHERE source = 'email'
            AND status = 'error'
            AND DATE(created_at) = ?
            """,
            (today,)
        )
        failed_today = cursor.fetchone()["count"]

        return processed_today, failed_today
    finally:
        pool.release_connection(conn)


@router.get("/email/status", response_model=EmailStatusResponse)
async def get_email_status(
    email_service: EmailIngestionService = Depends(get_email_service),
    app_settings: Settings = Depends(get_settings),
    auth: dict = Depends(require_scope("admin:config")),
):
    """
    Get email ingestion service status.

    Returns comprehensive status information about the email ingestion
    service, including health, statistics, and IMAP inbox status.

    Requires:
        - Valid Bearer token in Authorization header
        - Valid token with admin:config scope (server-side scope derivation)

    Returns:
        EmailStatusResponse with:
        - enabled: Whether email ingestion is enabled via imap_enabled setting
        - healthy: Whether the service is running without recent errors
        - last_poll: ISO timestamp of last poll, or None if never polled
        - emails_processed_today: Number of emails processed today
        - emails_failed_today: Number of emails that failed processing today
        - unseen_emails: Number of UNSEEN emails in IMAP inbox (-1 if query failed)
        - current_backoff_delay: Current backoff delay in seconds, or None if connected
    """
    # Check if email ingestion is enabled
    enabled = app_settings.imap_enabled

    # Get health status
    healthy = email_service.is_healthy()

    # Get last poll time
    last_poll_time = email_service.get_last_poll_time()
    last_poll = last_poll_time.isoformat() if last_poll_time else None

    # Get today's stats (processed and failed counts)
    processed_today, failed_today = await asyncio.to_thread(_get_today_stats, email_service.pool)

    # Get unseen email count from IMAP (if enabled)
    if enabled:
        unseen_emails = await _get_unseen_count(email_service)
    else:
        unseen_emails = 0

    # Get current backoff delay
    current_backoff_delay = email_service.get_current_backoff_delay()

    return EmailStatusResponse(
        enabled=enabled,
        healthy=healthy,
        last_poll=last_poll,
        emails_processed_today=processed_today,
        emails_failed_today=failed_today,
        unseen_emails=unseen_emails,
        current_backoff_delay=current_backoff_delay,
    )
