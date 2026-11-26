"""Unified reviewed-knowledge queue (issue #1163, Workstream L PR 7).

One review queue across the wiki and the knowledge base: write-access
callers see wiki pages (status ``needs_review``) and compiled KMS entries
(status ``draft``) awaiting review in one list, each beside the source
quote it rests on; a batch approve/reject updates every item or none.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import get_current_active_user, get_db, get_evaluate_policy
from app.models.database import transaction_context
from app.security import csrf_protect
from app.services.kms_store import KMSStore
from app.services.wiki_store import WikiStore

router = APIRouter()

# Excerpt length for the source quote each queue item rests on (#1163 AC4).
_SOURCE_QUOTE_CHARS = 200


async def _require_vault_write(
    evaluate: Callable, user: dict, vault_id: int
) -> None:
    if not await evaluate(user, "vault", vault_id, "write"):
        raise HTTPException(status_code=403, detail="No write access to this vault")


class ReviewQueueItem(BaseModel):
    kind: str = Field(..., description="'wiki_page' | 'kms_entry'")
    id: int


class ReviewQueueBatchRequest(BaseModel):
    vault_id: int
    items: List[ReviewQueueItem] = Field(..., min_length=1)
    action: str = Field(..., description="'approve' | 'reject'")


def _wiki_source_quote(db: sqlite3.Connection, vault_id: int, page_id: int) -> Optional[str]:
    """The first claim-source quote for a page, else an attached-file excerpt."""
    quote_row = db.execute(
        """
        SELECT wcs.quote FROM wiki_claim_sources wcs
        JOIN wiki_claims wc ON wc.id = wcs.claim_id
        WHERE wc.vault_id = ? AND wc.page_id = ? AND wcs.quote IS NOT NULL
              AND wcs.quote != ''
        ORDER BY wcs.id LIMIT 1
        """,
        (vault_id, page_id),
    ).fetchone()
    if quote_row and quote_row[0]:
        return str(quote_row[0])[:_SOURCE_QUOTE_CHARS]
    file_row = db.execute(
        """
        SELECT f.parsed_text FROM wiki_page_files wpf
        JOIN files f ON f.id = wpf.file_id
        WHERE wpf.vault_id = ? AND wpf.page_id = ? AND f.parsed_text IS NOT NULL
        ORDER BY wpf.id LIMIT 1
        """,
        (vault_id, page_id),
    ).fetchone()
    if file_row and file_row[0]:
        return str(file_row[0])[:_SOURCE_QUOTE_CHARS]
    return None


def _kms_source_quote(db: sqlite3.Connection, entry_row: sqlite3.Row) -> Optional[str]:
    parsed = entry_row["parsed_text"] if "parsed_text" in entry_row.keys() else None
    if parsed:
        return str(parsed)[:_SOURCE_QUOTE_CHARS]
    return None


@router.get("/review/queue")
async def get_review_queue(
    vault_id: int = Query(...),
    kind: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """Wiki pages and KMS entries awaiting review, in one list (#1163 AC1)."""
    await _require_vault_write(evaluate, user, vault_id)

    items: List[dict] = []
    if kind in (None, "wiki_page"):
        rows = db.execute(
            """
            SELECT id, slug, title, status, updated_at FROM wiki_pages
            WHERE vault_id = ? AND status = 'needs_review' ORDER BY updated_at DESC, id
            """,
            (vault_id,),
        ).fetchall()
        for row in rows:
            items.append(
                {
                    "kind": "wiki_page",
                    "id": row["id"],
                    "title": row["title"],
                    "slug": row["slug"],
                    "status": row["status"],
                    "file_id": None,
                    "updated_at": row["updated_at"],
                    "source_quote": _wiki_source_quote(db, vault_id, row["id"]),
                }
            )
    if kind in (None, "kms_entry"):
        rows = db.execute(
            """
            SELECT k.id, k.slug, k.title, k.status, k.updated_at, k.file_id,
                   f.parsed_text AS parsed_text
            FROM kms_entries k
            LEFT JOIN files f ON f.id = k.file_id
            WHERE k.vault_id = ? AND k.status = 'draft' AND k.source_type = 'document'
            ORDER BY k.updated_at DESC, k.id
            """,
            (vault_id,),
        ).fetchall()
        for row in rows:
            items.append(
                {
                    "kind": "kms_entry",
                    "id": row["id"],
                    "title": row["title"],
                    "slug": row["slug"],
                    "status": row["status"],
                    "file_id": row["file_id"],
                    "updated_at": row["updated_at"],
                    "source_quote": _kms_source_quote(db, row),
                }
            )
    if status:
        items = [i for i in items if i["status"] == status]
    return {"items": items, "count": len(items)}


@router.post("/review/queue/batch")
async def batch_review_queue(
    request: ReviewQueueBatchRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """Approve/reject several queue items in one all-or-none batch (#1163 AC2).

    Every item is validated as an eligible queue target (exists, correct
    vault, correct kind, KMS document source, CURRENTLY awaiting review)
    INSIDE the transaction before any write: a foreign, missing, or
    wrong-status item rolls the whole batch back. Mutations run through
    store operations so versions, snapshots, and activity logs match the
    single-item flows.
    """
    await _require_vault_write(evaluate, user, request.vault_id)
    if request.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be approve or reject")

    wiki_store = WikiStore(db)
    kms_store = KMSStore(db)
    seen: set[tuple[str, int]] = set()

    with transaction_context(db):
        for item in request.items:
            key = (item.kind, item.id)
            if key in seen:
                raise HTTPException(status_code=400, detail="duplicate queue item")
            seen.add(key)
            if item.kind == "wiki_page":
                page = wiki_store.get_page(item.id, load_relations=False)
                if page is None or page.vault_id != request.vault_id:
                    raise HTTPException(status_code=404, detail="Wiki page not found")
                if page.status != "needs_review":
                    raise HTTPException(
                        status_code=400,
                        detail="wiki page is not awaiting review",
                    )
                new_status = "verified" if request.action == "approve" else "archived"
                wiki_store.update_page(
                    page_id=item.id,
                    vault_id=request.vault_id,
                    status=new_status,
                    commit=False,
                )
            elif item.kind == "kms_entry":
                entry = kms_store.get_entry(item.id)
                if entry is None or entry.vault_id != request.vault_id:
                    raise HTTPException(status_code=404, detail="KMS entry not found")
                if entry.source_type != "document":
                    raise HTTPException(
                        status_code=400,
                        detail="kms entry is not a compiled document entry",
                    )
                if entry.status != "draft":
                    raise HTTPException(
                        status_code=400,
                        detail="kms entry is not awaiting review",
                    )
                new_status = "published" if request.action == "approve" else "archived"
                kms_store.update_entry(
                    item.id,
                    request.vault_id,
                    status=new_status,
                    commit=False,
                )
            else:
                raise HTTPException(status_code=400, detail="unknown queue item kind")

    return {
        "action": request.action,
        "updated": len(seen),
        "items": [{"kind": k, "id": i, "status": (
            ("verified" if request.action == "approve" else "archived")
            if k == "wiki_page"
            else ("published" if request.action == "approve" else "archived")
        )} for k, i in sorted(seen)],
    }
