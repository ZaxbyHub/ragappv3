"""Onboarding milestone API routes (issue #782, UI-ENH-07 stage 2).

First-run checklist milestones, derived from real server state rather than
client-only heuristics:

- ``vault_created``         — the user owns at least one vault (``vaults.owner_id``
                              is the creator column; ``vault_members`` rows alone
                              are membership, not creation).
- ``upload_indexed``        — at least one file in a vault the user owns reached
                              status ``indexed``. Strict per the acceptance
                              criterion ("one upload reach ``indexed``"):
                              a scan/sync-path partial success lands ``indexed``
                              with ``partial_embeddings=1`` and counts, while an
                              upload/reindex-path partial lands ``partial`` and
                              does not (document_processor.py
                              ``_update_processing_status``).
- ``first_question_asked``  — at least one role='user' chat message in a session
                              owned by the user (``chat_sessions.user_id``).
- ``first_citation_opened`` — nothing else records this; a per-user row in
                              ``user_onboarding_state`` persists it (audit
                              review correction C4).

``show_checklist`` hides the checklist once every milestone is complete or the
user explicitly dismissed it.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import get_current_active_user, get_db
from app.limiter import limiter
from app.security import csrf_protect

logger = logging.getLogger(__name__)

router = APIRouter()


class OnboardingMilestones(BaseModel):
    vault_created: bool
    upload_indexed: bool
    first_question_asked: bool
    first_citation_opened: bool
    show_checklist: bool


class OnboardingWriteAck(BaseModel):
    ok: bool


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple) -> sqlite3.Row | None:
    cursor = conn.execute(sql, params)
    return cursor.fetchone()


@router.get("/onboarding/milestones", response_model=OnboardingMilestones)
async def get_onboarding_milestones(
    user: dict = Depends(get_current_active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> OnboardingMilestones:
    user_id = user["id"]

    vault_row = await asyncio.to_thread(
        _fetch_one,
        conn,
        "SELECT 1 FROM vaults WHERE owner_id = ? LIMIT 1",
        (user_id,),
    )
    upload_row = await asyncio.to_thread(
        _fetch_one,
        conn,
        "SELECT 1 FROM files f JOIN vaults v ON f.vault_id = v.id "
        "WHERE v.owner_id = ? AND f.status = 'indexed' LIMIT 1",
        (user_id,),
    )
    question_row = await asyncio.to_thread(
        _fetch_one,
        conn,
        "SELECT 1 FROM chat_messages cm JOIN chat_sessions cs "
        "ON cm.session_id = cs.id "
        "WHERE cs.user_id = ? AND cm.role = 'user' LIMIT 1",
        (user_id,),
    )
    state_row = await asyncio.to_thread(
        _fetch_one,
        conn,
        "SELECT citation_opened_at, checklist_dismissed_at "
        "FROM user_onboarding_state WHERE user_id = ?",
        (user_id,),
    )

    vault_created = vault_row is not None
    upload_indexed = upload_row is not None
    first_question_asked = question_row is not None
    first_citation_opened = bool(
        state_row is not None and state_row["citation_opened_at"] is not None
    )
    dismissed = bool(
        state_row is not None and state_row["checklist_dismissed_at"] is not None
    )

    all_complete = (
        vault_created
        and upload_indexed
        and first_question_asked
        and first_citation_opened
    )
    return OnboardingMilestones(
        vault_created=vault_created,
        upload_indexed=upload_indexed,
        first_question_asked=first_question_asked,
        first_citation_opened=first_citation_opened,
        show_checklist=not dismissed and not all_complete,
    )


def _record_citation_opened(conn: sqlite3.Connection, user_id: int) -> None:
    """Insert/update the user's state row; the FIRST citation-open timestamp wins."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO user_onboarding_state "
        "(user_id, citation_opened_at, checklist_dismissed_at, updated_at) "
        "VALUES (?, ?, NULL, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "citation_opened_at = COALESCE("
        "user_onboarding_state.citation_opened_at, excluded.citation_opened_at), "
        "updated_at = excluded.updated_at",
        (user_id, now, now),
    )
    conn.commit()


def _record_dismissal(conn: sqlite3.Connection, user_id: int) -> None:
    """Insert/update the user's state row; the FIRST dismissal timestamp wins."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO user_onboarding_state "
        "(user_id, citation_opened_at, checklist_dismissed_at, updated_at) "
        "VALUES (?, NULL, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "checklist_dismissed_at = COALESCE("
        "user_onboarding_state.checklist_dismissed_at, "
        "excluded.checklist_dismissed_at), "
        "updated_at = excluded.updated_at",
        (user_id, now, now),
    )
    conn.commit()


@router.post(
    "/onboarding/milestones/citation-opened", response_model=OnboardingWriteAck
)
@limiter.limit("5/hour")
async def mark_citation_opened(
    request: Request,
    user: dict = Depends(get_current_active_user),
    conn: sqlite3.Connection = Depends(get_db),
    _csrf: str = Depends(csrf_protect),
) -> OnboardingWriteAck:
    try:
        await asyncio.to_thread(_record_citation_opened, conn, user["id"])
    except sqlite3.Error:
        logger.exception(
            "Failed to record citation-opened milestone for user %s", user["id"]
        )
        raise HTTPException(status_code=503, detail="Milestone storage unavailable")
    return OnboardingWriteAck(ok=True)


@router.post("/onboarding/milestones/dismiss", response_model=OnboardingWriteAck)
@limiter.limit("5/hour")
async def dismiss_checklist(
    request: Request,
    user: dict = Depends(get_current_active_user),
    conn: sqlite3.Connection = Depends(get_db),
    _csrf: str = Depends(csrf_protect),
) -> OnboardingWriteAck:
    try:
        await asyncio.to_thread(_record_dismissal, conn, user["id"])
    except sqlite3.Error:
        logger.exception(
            "Failed to record checklist dismissal for user %s", user["id"]
        )
        raise HTTPException(status_code=503, detail="Milestone storage unavailable")
    return OnboardingWriteAck(ok=True)
