"""
Wiki / Knowledge Compiler API routes.

All endpoints are vault-scoped. Access follows vault access permissions.
"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import asdict
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    require_model_ready,
)
from app.config import settings
from app.security import csrf_protect
from app.services.wiki_compiler import WikiCompiler
from app.services.wiki_events import get_wiki_event_bus
from app.services.wiki_linter import WikiLinter
from app.services.wiki_store import WikiStore

logger = logging.getLogger(__name__)


async def require_wiki_enabled() -> None:
    """Master switch (config.wiki_enabled). When off, the entire wiki subsystem —
    reads, writes, and job creation — is unavailable.

    Returns 503 (Service Unavailable) since the subsystem is intentionally
    turned off, not an authorization failure.
    """
    if not settings.wiki_enabled:
        raise HTTPException(status_code=503, detail="Wiki subsystem is disabled")


router = APIRouter(dependencies=[Depends(require_wiki_enabled)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_dict(obj) -> dict:
    try:
        return asdict(obj)
    except Exception:
        return obj if isinstance(obj, dict) else {}


def _page_dict(page) -> dict:
    if page is None:
        return {}
    d = _as_dict(page)
    d["claims"] = [_as_dict(c) for c in (page.claims or [])]
    d["entities"] = [_as_dict(e) for e in (page.entities or [])]
    d["lint_findings"] = [_as_dict(f) for f in (page.lint_findings or [])]
    return d


async def _require_vault_read(db: sqlite3.Connection, user: dict, vault_id: int) -> None:
    evaluate = get_evaluate_policy(db)
    if not await evaluate(user, "vault", vault_id, "read"):
        raise HTTPException(status_code=403, detail="No read access to this vault")


async def _require_vault_write(db: sqlite3.Connection, user: dict, vault_id: int) -> None:
    evaluate = get_evaluate_policy(db)
    if not await evaluate(user, "vault", vault_id, "write"):
        raise HTTPException(status_code=403, detail="No write access to this vault")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class WikiPageCreateRequest(BaseModel):
    vault_id: int
    title: str = Field(..., min_length=1, max_length=500)
    page_type: str
    slug: Optional[str] = None
    markdown: str = ""
    summary: str = ""
    status: str = "draft"
    confidence: float = 0.0
    parent_id: Optional[int] = None


class WikiPageUpdateRequest(BaseModel):
    title: Optional[str] = None
    page_type: Optional[str] = None
    slug: Optional[str] = None
    markdown: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None
    confidence: Optional[float] = None
    parent_id: Optional[int] = None
    expected_version: Optional[int] = None


class WikiClaimCreateRequest(BaseModel):
    vault_id: int
    claim_text: str = Field(..., min_length=1, max_length=2000)
    source_type: str
    page_id: Optional[int] = None
    claim_type: str = "fact"
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object: Optional[str] = None
    status: str = "active"
    confidence: float = 0.0


class WikiClaimUpdateRequest(BaseModel):
    claim_text: Optional[str] = None
    source_type: Optional[str] = None
    page_id: Optional[int] = None
    claim_type: Optional[str] = None
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object: Optional[str] = None
    status: Optional[str] = None
    confidence: Optional[float] = None


class PromoteMemoryRequest(BaseModel):
    memory_id: int
    vault_id: int
    page_type: Optional[str] = None
    target_page_id: Optional[int] = None
    status: str = "needs_review"


class LintRunRequest(BaseModel):
    vault_id: int


class WikiFileAttachRequest(BaseModel):
    vault_id: int
    file_id: int


class WikiBulkRequest(BaseModel):
    vault_id: int
    page_ids: list[int] = Field(..., min_length=1)
    action: str = Field(..., description="Action: delete or update")
    updates: Optional[dict] = None


# ---------------------------------------------------------------------------
# Pages endpoints
# ---------------------------------------------------------------------------

@router.get("/wiki/pages")
async def list_wiki_pages(
    vault_id: int = Query(..., description="Vault ID"),
    page_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    pages = store.list_pages(vault_id, page_type=page_type, status=status, search=search, page=page, per_page=per_page)
    return {"pages": [_as_dict(p) for p in pages], "page": page, "per_page": per_page}


@router.post("/wiki/pages", status_code=201)
async def create_wiki_page(
    request: WikiPageCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, request.vault_id)
    store = WikiStore(db)
    try:
        page = store.create_page(
            vault_id=request.vault_id,
            title=request.title,
            page_type=request.page_type,
            slug=request.slug,
            markdown=request.markdown,
            summary=request.summary,
            status=request.status,
            confidence=request.confidence,
            created_by=user.get("id"),
            parent_id=request.parent_id,
        )
    except Exception:
        logger.exception("Error creating wiki page")
        raise HTTPException(status_code=400, detail="Failed to create wiki page")
    return _page_dict(page)


@router.get("/wiki/pages/{page_id}")
async def get_wiki_page(
    page_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    store = WikiStore(db)
    page = store.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    # F-003: collapse a vault-access denial to 404 so an unauthorized caller cannot
    # distinguish "exists in another vault" (403) from "does not exist" (404).
    try:
        await _require_vault_read(db, user, page.vault_id)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise HTTPException(status_code=404, detail="Wiki page not found")
        raise
    return _page_dict(page)


@router.put("/wiki/pages/{page_id}")
async def update_wiki_page(
    page_id: int,
    request: WikiPageUpdateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    store = WikiStore(db)
    page = store.get_page(page_id, load_relations=False)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    # F-003: collapse a vault-access denial to 404 (existence non-distinguishable).
    try:
        await _require_vault_write(db, user, page.vault_id)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise HTTPException(status_code=404, detail="Wiki page not found")
        raise
    updates = request.model_dump(exclude_none=True)
    expected_version = updates.pop("expected_version", None)
    try:
        updated = store.update_page(
            page_id,
            page.vault_id,
            expected_version=expected_version,
            edited_by=user.get("id"),
            **updates,
        )
    except ValueError as e:
        if "Version conflict" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    result = _page_dict(updated)
    result["version"] = updated.version if updated else None
    return result


@router.delete("/wiki/pages/{page_id}", status_code=204)
async def delete_wiki_page(
    page_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    store = WikiStore(db)
    page = store.get_page(page_id, load_relations=False)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    # F-003: collapse a vault-access denial to 404 (existence non-distinguishable).
    try:
        await _require_vault_write(db, user, page.vault_id)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise HTTPException(status_code=404, detail="Wiki page not found")
        raise
    store.delete_page(page_id, page.vault_id)


# ---------------------------------------------------------------------------
# Page version history (DD-C003)
# ---------------------------------------------------------------------------

@router.get("/wiki/pages/{page_id}/versions")
async def list_page_versions(
    page_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    page = store.get_page(page_id, load_relations=False)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    if page.vault_id != vault_id:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    versions = store.list_versions(page_id)
    return {"versions": [_as_dict(v) for v in versions]}


# ---------------------------------------------------------------------------
# Page file attachments (DD-C019)
# ---------------------------------------------------------------------------

@router.post("/wiki/pages/{page_id}/files", status_code=201)
async def attach_file_to_page(
    page_id: int,
    request: WikiFileAttachRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
    ):
    await _require_vault_write(db, user, request.vault_id)
    store = WikiStore(db)
    page = store.get_page(page_id, load_relations=False)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    if page.vault_id != request.vault_id:
        raise HTTPException(status_code=403, detail="Page does not belong to this vault")
    try:
        pf = store.attach_file(page_id, request.file_id, request.vault_id)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="File already attached to this page")
    except Exception:
        logger.exception("Error attaching file to wiki page")
        raise HTTPException(status_code=400, detail="Failed to attach file to page")
    return _as_dict(pf)


@router.delete("/wiki/pages/{page_id}/files/{file_id}", status_code=204)
async def detach_file_from_page(
    page_id: int,
    file_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, vault_id)
    store = WikiStore(db)
    removed = store.detach_file(page_id, file_id, vault_id)
    if not removed:
        raise HTTPException(status_code=404, detail="File attachment not found")


@router.get("/wiki/pages/{page_id}/files")
async def list_page_files(
    page_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    page = store.get_page(page_id, load_relations=False)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    if page.vault_id != vault_id:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    files = store.list_page_files(page_id)
    return {"files": [_as_dict(f) for f in files]}


# ---------------------------------------------------------------------------
# Backlinks (DD-C030)
# ---------------------------------------------------------------------------

@router.get("/wiki/pages/{page_id}/backlinks")
async def list_page_backlinks(
    page_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    page = store.get_page(page_id, load_relations=False)
    if not page:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    if page.vault_id != vault_id:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    backlinks = store.list_backlinks(page_id, vault_id)
    return {"backlinks": [_as_dict(bl) for bl in backlinks]}


# ---------------------------------------------------------------------------
# Activity feed (DD-C026)
# ---------------------------------------------------------------------------

@router.get("/wiki/activity")
async def list_wiki_activity(
    vault_id: int = Query(...),
    limit: int = Query(50, ge=1, le=200),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    entries = store.list_activity(vault_id, limit=limit)
    return {"activity": [_as_dict(e) for e in entries]}


# ---------------------------------------------------------------------------
# Bulk operations (DD-C025)
# ---------------------------------------------------------------------------

@router.post("/wiki/pages/bulk")
async def bulk_wiki_pages(
    request: WikiBulkRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, request.vault_id)
    store = WikiStore(db)
    if request.action == "delete":
        count = store.bulk_delete_pages(request.page_ids, request.vault_id)
        return {"action": "delete", "deleted": count}
    elif request.action == "update":
        if not request.updates:
            raise HTTPException(status_code=400, detail="updates field required for update action")
        pages = store.bulk_update_pages(request.page_ids, request.vault_id, **request.updates)
        return {"action": "update", "updated": len(pages), "pages": [_as_dict(p) for p in pages]}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown bulk action: {request.action}")


# ---------------------------------------------------------------------------
# Entities endpoints
# ---------------------------------------------------------------------------

@router.get("/wiki/entities")
async def list_wiki_entities(
    vault_id: int = Query(...),
    search: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    entities = store.list_entities(vault_id, search=search, limit=limit, offset=offset)
    return {"entities": [_as_dict(e) for e in entities]}


# ---------------------------------------------------------------------------
# Claims endpoints
# ---------------------------------------------------------------------------

@router.get("/wiki/claims")
async def list_wiki_claims(
    vault_id: int = Query(...),
    page_id: Optional[int] = Query(None),
    entity: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    claims = store.list_claims(
        vault_id, page_id=page_id, entity=entity, search=search, status=status,
        limit=limit, offset=offset,
    )
    return {"claims": [_as_dict(c) for c in claims]}


@router.post("/wiki/claims", status_code=201)
async def create_wiki_claim(
    request: WikiClaimCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, request.vault_id)
    store = WikiStore(db)
    claim = store.create_claim(
        vault_id=request.vault_id,
        claim_text=request.claim_text,
        source_type=request.source_type,
        page_id=request.page_id,
        claim_type=request.claim_type,
        subject=request.subject,
        predicate=request.predicate,
        object=request.object,
        status=request.status,
        confidence=request.confidence,
        created_by=user.get("id"),
    )
    return _as_dict(claim)


@router.put("/wiki/claims/{claim_id}")
async def update_wiki_claim(
    claim_id: int,
    request: WikiClaimUpdateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    store = WikiStore(db)
    claim = store.get_claim(claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    await _require_vault_write(db, user, claim.vault_id)
    updates = request.model_dump(exclude_none=True)

    # PR C: server-side source-quote re-verification on transitions to
    # 'active'. Curator-authored claims default to 'needs_review' or
    # 'active' depending on the curator mode setting; the operator can
    # later promote a needs_review claim via this PUT, but ONLY if the
    # source_quote stored on the claim's source row still matches the
    # underlying chunk text. Otherwise the source has changed (or was
    # never verifiable in the first place) and we must reject the
    # promotion rather than silently activate an unverifiable claim.
    new_status = updates.get("status")
    if (
        new_status == "active"
        and claim.status != "active"
        and claim.created_by_kind == "llm_curator"
    ):
        from app.services.wiki_curator import verify_quote

        # Fetch every source row for this claim and require at least
        # one verifiable quote against the file's parsed_text.
        source_rows = await asyncio.to_thread(
            lambda: db.execute(
                "SELECT * FROM wiki_claim_sources WHERE claim_id = ?",
                (claim_id,),
            ).fetchall()
        )
        verified = False
        any_file_source = False
        any_quote = False
        for src_row in source_rows:
            try:
                src = dict(src_row) if hasattr(src_row, "keys") else {}
            except Exception:
                src = {}
            quote = src.get("quote")
            file_id = src.get("file_id")
            if quote:
                any_quote = True
            if not quote or file_id is None:
                continue
            any_file_source = True
            file_row = await asyncio.to_thread(
                lambda fid=int(file_id): db.execute(
                    "SELECT parsed_text FROM files WHERE id = ?",
                    (fid,),
                ).fetchone()
            )
            if not file_row:
                continue
            try:
                parsed_text = file_row["parsed_text"]
            except (KeyError, IndexError, TypeError):
                parsed_text = file_row[0] if len(file_row) else None
            if not parsed_text:
                continue
            if verify_quote(quote, parsed_text):
                verified = True
                break
        if not verified:
            # Distinguish "verifiable but mismatched" from "no file
            # source attached" so the operator gets an actionable
            # message rather than a generic 400. Curator claims
            # authored on the query/manual trigger have file_id=None
            # on every source row and can NEVER be auto-promoted to
            # active — they must be edited manually or replaced.
            if not any_file_source:
                detail = (
                    "Cannot auto-activate curator-authored claim: no source "
                    "row references a document file (this claim was likely "
                    "produced from a chat-query or manual-promote curator "
                    "trigger). Edit the claim and attach a file source, or "
                    "leave it in 'needs_review' status for manual handling."
                )
            elif not any_quote:
                detail = (
                    "Cannot auto-activate curator-authored claim: no source "
                    "row carries a stored quote. Re-run the wiki compile "
                    "or attach a verifiable source."
                )
            else:
                detail = (
                    "Cannot auto-activate curator-authored claim: source_quote "
                    "no longer verifiable in any associated source. The "
                    "underlying document may have changed; re-run the wiki "
                    "compile or attach a new verifiable source."
                )
            raise HTTPException(status_code=400, detail=detail)

    updated = store.update_claim(claim_id, claim.vault_id, **updates)
    return _as_dict(updated)


@router.delete("/wiki/claims/{claim_id}", status_code=204)
async def delete_wiki_claim(
    claim_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    store = WikiStore(db)
    claim = store.get_claim(claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    await _require_vault_write(db, user, claim.vault_id)
    store.delete_claim(claim_id, claim.vault_id)


# ---------------------------------------------------------------------------
# Lint endpoints
# ---------------------------------------------------------------------------

@router.get("/wiki/lint")
async def get_lint_findings(
    vault_id: int = Query(...),
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    findings = store.list_lint_findings(vault_id, status=status, severity=severity)
    return {"findings": [_as_dict(f) for f in findings]}


@router.post("/wiki/lint/run")
async def run_wiki_lint(
    request: LintRunRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, request.vault_id)
    store = WikiStore(db)
    linter = WikiLinter(db, store)
    findings = linter.run_lint(request.vault_id)
    return {"findings": [_as_dict(f) for f in findings], "count": len(findings)}


# ---------------------------------------------------------------------------
# Promote memory endpoint
# ---------------------------------------------------------------------------

@router.post("/wiki/promote-memory")
async def promote_memory_to_wiki(
    request: PromoteMemoryRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, request.vault_id)
    store = WikiStore(db)
    compiler = WikiCompiler(db, store)
    try:
        # Run the synchronous compiler off the event loop. promote_memory does
        # sync entity extraction + DB writes and, when the curator is enabled,
        # blocks up to wiki_llm_curator_timeout_sec via the curator offload
        # executor (issue #276 A6-2). Thread-safe: the request-scoped db
        # connection is opened with check_same_thread=False and the pool hands
        # out exclusive connections, matching the to_thread pattern at 531/552.
        result = await asyncio.to_thread(
            compiler.promote_memory,
            memory_id=request.memory_id,
            vault_id=request.vault_id,
            page_type=request.page_type,
            target_page_id=request.target_page_id,
            status=request.status,
            created_by=user.get("id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception:
        logger.exception("Error promoting memory to wiki")
        raise HTTPException(status_code=500, detail="Failed to promote memory to wiki")

    return {
        "page": _page_dict(result["page"]),
        "claims": [_as_dict(c) for c in result["claims"]],
        "entities": [_as_dict(e) for e in result["entities"]],
        "relations": [_as_dict(r) for r in result["relations"]],
    }


# ---------------------------------------------------------------------------
# Jobs endpoints
# ---------------------------------------------------------------------------

@router.get("/wiki/jobs")
async def list_wiki_jobs(
    vault_id: int = Query(...),
    status: Optional[str] = Query(None),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    jobs = store.list_jobs(vault_id, status=status)
    return {"jobs": [_as_dict(j) for j in jobs]}


@router.get("/wiki/events")
async def wiki_events_stream(
    vault_id: int = Query(...),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """SSE stream of terminal-state wiki compile job events for a vault.

    Emits ``data: {json}\\n\\n`` lines whenever a wiki compile job finishes
    (completed / permanently failed). Clients refetch the canonical state via
    the existing REST endpoints on each event. A 15-second keepalive comment
    keeps proxies and load balancers from idling the connection out.
    """
    if not await evaluate(user, "vault", vault_id, "read"):
        raise HTTPException(status_code=403, detail="No read access to this vault")

    bus = get_wiki_event_bus()
    queue = bus.subscribe(vault_id)

    async def event_generator():
        try:
            # Hello event so the client has positive proof of subscription.
            yield f"data: {json.dumps({'type': 'subscribed', 'vault_id': vault_id})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Comment-line keepalive (ignored by EventSource clients).
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe(vault_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------

@router.get("/wiki/search")
async def search_wiki(
    vault_id: int = Query(...),
    q: str = Query(..., min_length=1),
    page_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    sort_by: Optional[str] = Query(None, description="Sort pages by: title, updated_at, confidence"),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_model_ready),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    results = store.search(vault_id, q, page_type=page_type, status=status, sort_by=sort_by)
    return {
        "query": q,
        "pages": [_as_dict(p) for p in results["pages"]],
        "claims": [_as_dict(c) for c in results["claims"]],
        "entities": [_as_dict(e) for e in results["entities"]],
    }


# ---------------------------------------------------------------------------
# Job management routes
# ---------------------------------------------------------------------------

@router.get("/wiki/jobs/{job_id}")
async def get_wiki_job(
    job_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    job = store.get_job(job_id, vault_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _as_dict(job)


@router.post("/wiki/jobs/{job_id}/retry", status_code=200)
async def retry_wiki_job(
    job_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, vault_id)
    store = WikiStore(db)
    job = store.retry_job(job_id, vault_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} not found or not in 'failed' state",
        )
    return _as_dict(job)


@router.post("/wiki/jobs/{job_id}/cancel", status_code=200)
async def cancel_wiki_job(
    job_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, vault_id)
    store = WikiStore(db)
    cancelled = store.cancel_job(job_id, vault_id)
    if not cancelled:
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} not found or not cancellable",
        )
    return {"job_id": job_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# Document wiki status routes
# ---------------------------------------------------------------------------

@router.post("/wiki/documents/{file_id}/compile", status_code=202)
async def compile_document_wiki(
    file_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    """Manually enqueue a wiki ingest job for an already-indexed document."""
    await _require_vault_write(db, user, vault_id)
    db.row_factory = sqlite3.Row
    file_row = db.execute(
        "SELECT id, file_name FROM files WHERE id = ? AND vault_id = ?",
        (file_id, vault_id),
    ).fetchone()
    if not file_row:
        raise HTTPException(status_code=404, detail=f"File {file_id} not found in vault {vault_id}")
    store = WikiStore(db)
    job = store.create_job(
        vault_id=vault_id,
        trigger_type="ingest",
        trigger_id=f"file:{file_id}",
        input_json={"file_id": file_id, "vault_id": vault_id},
    )
    return {"job_id": job.id, "status": job.status}


@router.get("/wiki/documents/{file_id}/status")
async def get_document_wiki_status(
    file_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    """Return semantic wiki status, counts, linked pages/claims, and latest job for a document."""
    await _require_vault_read(db, user, vault_id)
    db.row_factory = sqlite3.Row
    store = WikiStore(db)

    # Collect ingest jobs for this file (filtered + bounded in SQL — F-012)
    file_jobs = store.list_jobs(
        vault_id,
        trigger_type="ingest",
        trigger_id=f"file:{file_id}",
        limit=50,
    )
    latest_job = file_jobs[0] if file_jobs else None

    # Derive semantic status
    if latest_job is None:
        wiki_status = "not_compiled"
    elif latest_job.status == "running":
        wiki_status = "compiling"
    elif latest_job.status == "failed":
        wiki_status = "failed"
    elif latest_job.status == "cancelled":
        wiki_status = "not_compiled"
    elif latest_job.status == "completed":
        try:
            result = json.loads(latest_job.result_json or "{}")
        except (json.JSONDecodeError, TypeError):
            result = {}
        if result.get("skipped"):
            wiki_status = "skipped"
        else:
            wiki_status = "compiled"
    else:
        wiki_status = "not_compiled"

    # Count claims sourced from this file
    claims_rows = db.execute(
        """SELECT wc.id, wc.status, wc.page_id
           FROM wiki_claims wc
           JOIN wiki_claim_sources wcs ON wcs.claim_id = wc.id
           WHERE wcs.file_id = ? AND wc.vault_id = ?""",
        (file_id, vault_id),
    ).fetchall()
    claims_total = len(claims_rows)
    active_claims = sum(1 for r in claims_rows if dict(r)["status"] == "active")

    # Collect linked page IDs
    page_ids = {dict(r)["page_id"] for r in claims_rows if dict(r)["page_id"]}
    pages_info = []
    for pid in page_ids:
        row = db.execute(
            "SELECT id, slug, title, page_type, status FROM wiki_pages WHERE id = ? AND vault_id = ?",
            (pid, vault_id),
        ).fetchone()
        if row:
            pages_info.append(dict(row))

    # A completed job with zero extracted claims is "skipped" (no extractable knowledge)
    if wiki_status == "compiled" and claims_total == 0 and not pages_info:
        wiki_status = "skipped"

    # Count open lint findings related to linked pages
    lint_count = 0
    if page_ids:
        lint_rows = db.execute(
            "SELECT related_page_ids_json FROM wiki_lint_findings WHERE vault_id = ? AND status = 'open'",
            (vault_id,),
        ).fetchall()
        for lr in lint_rows:
            try:
                related = json.loads(dict(lr)["related_page_ids_json"] or "[]")
                if any(pid in page_ids for pid in related):
                    lint_count += 1
            except (json.JSONDecodeError, TypeError):
                pass

    return {
        "file_id": file_id,
        "wiki_status": wiki_status,
        "pages_count": len(pages_info),
        "claims_count": claims_total,
        "active_claims": active_claims,
        "lint_count": lint_count,
        "pages": pages_info,
        "latest_job": _as_dict(latest_job) if latest_job else None,
        "job_count": len(file_jobs),
    }


# ---------------------------------------------------------------------------
# Memory wiki status route
# ---------------------------------------------------------------------------

@router.get("/wiki/memories/{memory_id}/status")
async def get_memory_wiki_status(
    memory_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    """Return semantic wiki status, linked pages/claims, and latest job for a memory record."""
    await _require_vault_read(db, user, vault_id)
    db.row_factory = sqlite3.Row
    store = WikiStore(db)

    # Collect memory jobs (trigger_id filtered + bounded in SQL — F-012).
    # trigger_type can be either "memory" or "manual", so keep that check in Python
    # over the already-bounded result set.
    jobs = store.list_jobs(vault_id, trigger_id=f"memory:{memory_id}", limit=50)
    mem_jobs = [j for j in jobs if j.trigger_type in ("memory", "manual")]
    latest_job = mem_jobs[0] if mem_jobs else None

    # Claims sourced from this memory
    claims_rows = db.execute(
        """SELECT wc.id, wc.status, wc.page_id, wc.claim_text
           FROM wiki_claims wc
           JOIN wiki_claim_sources wcs ON wcs.claim_id = wc.id
           WHERE wcs.memory_id = ? AND wc.vault_id = ?""",
        (memory_id, vault_id),
    ).fetchall()

    claims_data = [dict(r) for r in claims_rows]
    active_claims = sum(1 for c in claims_data if c["status"] == "active")
    stale_claims = sum(1 for c in claims_data if c["status"] == "superseded")

    # Linked pages
    page_ids = {c["page_id"] for c in claims_data if c["page_id"]}
    linked_pages = []
    for pid in page_ids:
        row = db.execute(
            "SELECT id, slug, title, page_type, status FROM wiki_pages WHERE id = ? AND vault_id = ?",
            (pid, vault_id),
        ).fetchone()
        if row:
            linked_pages.append(dict(row))

    # Semantic status
    if latest_job and latest_job.status == "running":
        wiki_status = "promoting"
    elif stale_claims > 0 and active_claims == 0:
        wiki_status = "stale"
    elif active_claims > 0:
        wiki_status = "promoted"
    elif len(claims_data) > 0:
        wiki_status = "promoted"
    else:
        wiki_status = "not_promoted"

    return {
        "memory_id": memory_id,
        "wiki_status": wiki_status,
        "claims_count": len(claims_data),
        "active_claims": active_claims,
        "stale_claims": stale_claims,
        "linked_pages": linked_pages,
        "latest_job": _as_dict(latest_job) if latest_job else None,
        "job_count": len(mem_jobs),
    }


# ---------------------------------------------------------------------------
# Relations route
# ---------------------------------------------------------------------------

@router.get("/wiki/relations")
async def list_wiki_relations(
    vault_id: int = Query(...),
    entity_id: Optional[int] = Query(None),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = WikiStore(db)
    relations = store.list_relations(vault_id=vault_id, entity_id=entity_id)
    return {"relations": [_as_dict(r) for r in relations]}


# ---------------------------------------------------------------------------
# Lint finding management
# ---------------------------------------------------------------------------

class LintFindingUpdateRequest(BaseModel):
    status: str = Field(..., description="New status: resolved, dismissed, or acknowledged")


@router.post("/wiki/lint/{finding_id}/resolve", status_code=200)
async def resolve_lint_finding(
    finding_id: int,
    body: LintFindingUpdateRequest,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, vault_id)
    store = WikiStore(db)
    try:
        finding = store.update_lint_finding(finding_id, vault_id, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not finding:
        raise HTTPException(status_code=404, detail=f"Lint finding {finding_id} not found")
    return _as_dict(finding)


# ---------------------------------------------------------------------------
# Full vault recompile
# ---------------------------------------------------------------------------

@router.post("/wiki/recompile", status_code=202)
async def recompile_vault_wiki(
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    """Enqueue a settings_reindex job to re-derive all stale claims in a vault."""
    await _require_vault_write(db, user, vault_id)
    store = WikiStore(db)
    job = store.create_job(
        vault_id=vault_id,
        trigger_type="settings_reindex",
        trigger_id=f"vault:{vault_id}",
        input_json={"vault_id": vault_id},
    )
    return {"job_id": job.id, "status": job.status}
