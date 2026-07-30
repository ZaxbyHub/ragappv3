"""Draft Room HTTP API (issue #435, ``specs/draft-room/SPEC.md`` sections 7-9).

Private, owner-scoped drafting projects: project/brief CRUD, raw-input upload
and metadata (never `files`/LanceDB/Wiki/KMS), the durable parse-job lifecycle,
manual Markdown revisions, Markdown export, and an authenticated SSE
notification stream.

This module deliberately does NOT implement compile, findings, claims,
evidence, Ready, or promote — those ship in later PRs once the underlying
tables (``draft_job_stages``, ``draft_evidence``, ``draft_claims``,
``draft_findings``) and services exist. ``GET /capabilities`` advertises them
as unavailable rather than leaving a future client to guess.

Authorization (SPEC section 9.1) is evaluated on every request:

* Every draft is loaded by BOTH ``draft_id`` and ``created_by`` — an owner
  mismatch is 404, never 403, so draft existence is never disclosed across
  owners. ``DraftStore`` enforces this at the query level; routes never
  fetch-then-authorize.
* After ownership, most operations additionally require current vault
  ``read`` permission (403 ``vault_access_revoked`` otherwise). The
  exceptions — list (annotated, never suppressed), job cancel, and
  whole-draft delete — stay available after vault access is revoked because
  they only expose owner metadata or reduce private data (SPEC section 9.1
  rule 3).
* Every child route (input/job/revision) resolves exclusively through its
  owning draft via ``DraftStore``; a child ID alone is never sufficient
  authorization.

Error contract (SPEC section 8.3): store/storage exceptions are translated to
:class:`DraftRoomHTTPError`, whose handler (registered in ``app.main``)
renders ``{"detail": "...", "code": "...", "context": {...}}`` with
``context`` omitted when empty. Framework ``RequestValidationError`` is left
untouched — FastAPI's standard 422 shape still applies to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Annotated, Any, Callable, Generic, Optional, TypeVar

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.deps import (
    _evaluate_policy,
    _resolve_active_user,
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    log_db_released,
)
from app.config import settings
from app.limiter import limiter
from app.security import csrf_protect
from app.services.draft_deletion import DraftDeletionService
from app.services.draft_events import build_event, get_draft_event_bus
from app.services.draft_input_storage import (
    DraftInputStorage,
    DraftInputStorageError,
)
from app.services.draft_store import (
    DRAFT_MODES,
    DRAFT_TIERS,
    INPUT_AUTHORITIES,
    INPUT_ROLES,
    DraftConflictError,
    DraftInputRecord,
    DraftJobRecord,
    DraftNotFoundError,
    DraftRecord,
    DraftRevisionRecord,
    DraftStore,
    DraftStoreError,
    sha256_text,
)
from app.services.security_audit import safe_record_security_event
from app.services.upload_validation import secure_filename

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/draft-room", tags=["draft-room"])

T = TypeVar("T")

_PIECE_TYPES = ("article", "report", "brief", "press_release", "other")
_TRANSFORMATION_STRENGTHS = ("light", "moderate", "substantial")
_DRAFTING_PRIORITIES = ("manuscript", "vault", "balanced")


# ── error contract (SPEC section 8.3) ───────────────────────────────────────


class DraftRoomHTTPError(HTTPException):
    """Draft Room error: repo-standard ``detail`` plus stable ``code``/``context``.

    ``context`` holds only documented non-secret scalar identifiers (e.g.
    ``existing_input_id``); the exception handler registered in ``app.main``
    omits the key entirely when it is empty.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        code: str,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code
        self.context = context or {}


async def draft_room_exception_handler(
    request: Request, exc: DraftRoomHTTPError
) -> JSONResponse:
    """Render ``{"detail", "code", "context"?}`` for every :class:`DraftRoomHTTPError`.

    Registered in ``app.main`` above the SPA catch-all. ``context`` is
    omitted (not sent as ``{}``) when empty, matching SPEC section 8.3.
    """
    body: dict[str, Any] = {"detail": exc.detail, "code": exc.code}
    if exc.context:
        body["context"] = exc.context
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


def _map_store_error(exc: DraftStoreError) -> DraftRoomHTTPError:
    """Translate a :class:`DraftStoreError` into its documented HTTP status/code."""
    if isinstance(exc, DraftNotFoundError):
        status_code = 404
    elif exc.code == "limit_exceeded":
        status_code = 413
    elif exc.code == "validation_failed":
        status_code = 422
    elif isinstance(exc, DraftConflictError):
        status_code = 409
    else:
        status_code = 500
    context: dict[str, Any] = {}
    existing_input_id = getattr(exc, "existing_input_id", None)
    if existing_input_id is not None:
        context["existing_input_id"] = existing_input_id
    return DraftRoomHTTPError(status_code, str(exc), exc.code, context)


def _map_storage_error(exc: DraftInputStorageError) -> DraftRoomHTTPError:
    """Translate a :class:`DraftInputStorageError` into its documented HTTP status/code."""
    status_by_code = {
        "input_too_large": 413,
        "unsupported_input": 415,
        "invalid_storage_path": 400,
    }
    status_code = status_by_code.get(exc.code, 500)
    return DraftRoomHTTPError(status_code, str(exc), exc.code)


async def _run_store(func: Callable[[], T]) -> T:
    """Run a blocking ``DraftStore`` call off the event loop, mapping its errors."""
    try:
        return await asyncio.to_thread(func)
    except DraftStoreError as exc:
        raise _map_store_error(exc) from exc


def _require_enabled() -> None:
    """Gate create/edit/upload/revision-create behind ``draft_room_enabled``.

    Capability discovery, owner list/read/export, job cancel, and whole-draft
    delete stay available even when disabled so an owner can always inspect
    and clean up their private data (SPEC section 9.2).
    """
    if not settings.draft_room_enabled:
        raise DraftRoomHTTPError(503, "draft room is disabled", "draft_room_disabled")


async def _require_vault_read(evaluate: Callable, user: dict, vault_id: int) -> None:
    if not await evaluate(user, "vault", vault_id, "read"):
        raise DraftRoomHTTPError(
            403, "no read access to this vault", "vault_access_revoked"
        )


async def _vault_access_label(evaluate: Callable, user: dict, vault_id: int) -> str:
    """``'write' | 'read' | 'revoked'`` for a list-row annotation (never suppresses)."""
    if await evaluate(user, "vault", vault_id, "write"):
        return "write"
    if await evaluate(user, "vault", vault_id, "read"):
        return "read"
    return "revoked"


def get_draft_input_storage() -> DraftInputStorage:
    return DraftInputStorage(Path(settings.data_dir) / "draft-room")


# ── request/response contracts (SPEC sections 7, 8.1) ───────────────────────


_BriefItem = Annotated[str, Field(min_length=1, max_length=500)]


class DraftBrief(BaseModel):
    """Assignment brief, validated verbatim against SPEC section 7."""

    piece_type: str
    audience: str = Field(..., min_length=1, max_length=500)
    purpose: str = Field(..., min_length=1, max_length=1000)
    tone: str = Field("clear and direct", min_length=1, max_length=500)
    target_words: int = Field(..., ge=100, le=20000)
    transformation_strength: str
    primary_input_id: Optional[int] = None
    must_include: list[_BriefItem] = Field(default_factory=list, max_length=50)
    must_avoid: list[_BriefItem] = Field(default_factory=list, max_length=50)
    preserve_quotes: bool = True
    preserve_numbers: bool = True
    preserve_uncertainty: bool = True
    drafting_priority: str = "balanced"
    additional_instructions: str = Field("", max_length=4000)

    @field_validator("piece_type")
    @classmethod
    def _check_piece_type(cls, v: str) -> str:
        if v not in _PIECE_TYPES:
            raise ValueError(f"piece_type must be one of {_PIECE_TYPES}")
        return v

    @field_validator("transformation_strength")
    @classmethod
    def _check_transformation_strength(cls, v: str) -> str:
        if v not in _TRANSFORMATION_STRENGTHS:
            raise ValueError(
                f"transformation_strength must be one of {_TRANSFORMATION_STRENGTHS}"
            )
        return v

    @field_validator("drafting_priority")
    @classmethod
    def _check_drafting_priority(cls, v: str) -> str:
        if v not in _DRAFTING_PRIORITIES:
            raise ValueError(f"drafting_priority must be one of {_DRAFTING_PRIORITIES}")
        return v


class DraftCreateRequest(BaseModel):
    vault_id: int
    title: str = Field(..., min_length=1, max_length=300)
    mode: str
    tier: str = "standard"
    brief: DraftBrief

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        if v not in DRAFT_MODES:
            raise ValueError(f"mode must be one of {sorted(DRAFT_MODES)}")
        return v

    @field_validator("tier")
    @classmethod
    def _check_tier(cls, v: str) -> str:
        if v not in DRAFT_TIERS:
            raise ValueError(f"tier must be one of {sorted(DRAFT_TIERS)}")
        return v


class DraftUpdateRequest(BaseModel):
    lock_version: int
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    brief: Optional[DraftBrief] = None
    tier: Optional[str] = None

    @field_validator("tier")
    @classmethod
    def _check_tier(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in DRAFT_TIERS:
            raise ValueError(f"tier must be one of {sorted(DRAFT_TIERS)}")
        return v


class LockVersionRequest(BaseModel):
    lock_version: int


class LockedSpan(BaseModel):
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    sha256: str = Field(..., min_length=64, max_length=64)
    reason: str = Field(..., min_length=1, max_length=500)

    @model_validator(mode="after")
    def _check_range(self) -> "LockedSpan":
        if self.end <= self.start:
            raise ValueError("locked span end must be greater than start")
        return self


class InputUpdateRequest(BaseModel):
    role: Optional[str] = None
    authority: Optional[str] = None
    as_of_date: Optional[str] = None
    clear_as_of_date: bool = False
    locked_spans: Optional[list[LockedSpan]] = None

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in INPUT_ROLES:
            raise ValueError(f"role must be one of {sorted(INPUT_ROLES)}")
        return v

    @field_validator("authority")
    @classmethod
    def _check_authority(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in INPUT_AUTHORITIES:
            raise ValueError(f"authority must be one of {sorted(INPUT_AUTHORITIES)}")
        return v


class RevisionCreateRequest(BaseModel):
    base_revision_id: Optional[int] = Field(...)
    lock_version: int
    content_md: str = Field(..., min_length=1)


class DraftSummary(BaseModel):
    id: int
    vault_id: int
    vault_access: str
    title: str
    mode: str
    status: str
    tier: str
    lock_version: int
    current_revision_id: Optional[int]
    active_job_id: Optional[int]
    input_count: int
    open_blocker_count: int
    created_at: str
    updated_at: str
    ready_at: Optional[str]


class DraftInput(BaseModel):
    id: int
    role: str
    authority: str
    as_of_date: Optional[str]
    original_name: str
    extension: str
    media_type: Optional[str]
    size_bytes: int
    content_sha256: str
    parse_status: str
    parse_error: Optional[str]
    parsed_char_count: Optional[int]
    active_parse_job_id: Optional[int]
    last_parse_job_id: Optional[int]
    created_at: str


class DraftInputContent(BaseModel):
    input_id: int
    parse_status: str
    parsed_text: Optional[str]


class DraftJob(BaseModel):
    id: int
    draft_id: int
    job_type: str
    status: str
    start_stage: Optional[str]
    active_stage: Optional[str]
    progress_percent: float
    model_call_count: int
    max_model_calls: int
    retry_count: int
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]


class DraftRevisionSummary(BaseModel):
    id: int
    revision_no: int
    parent_revision_id: Optional[int]
    job_id: Optional[int]
    source: str
    content_sha256: str
    fact_status: str
    is_current: bool
    created_by: Optional[int]
    created_at: str


class DraftRevisionDetail(BaseModel):
    summary: DraftRevisionSummary
    content_md: str
    sections: list[Any]
    citations: list[Any]
    qa_summary: dict[str, Any]


class DraftDetail(BaseModel):
    summary: DraftSummary
    brief: dict[str, Any]
    inputs: list[DraftInput]
    current_revision_summary: Optional[DraftRevisionSummary]
    active_compile_job: Optional[DraftJob]
    revision_count: int
    evidence_count: int
    claim_counts_by_status: dict[str, int]
    finding_counts_by_severity: dict[str, int]


class DraftInputUploadResponse(BaseModel):
    input: DraftInput
    job: DraftJob


class DraftRoomCapabilities(BaseModel):
    enabled: bool
    modes: list[str]
    tiers: list[str]
    piece_types: list[str]
    transformation_strengths: list[str]
    limits: dict[str, Any]
    export_formats: list[str]
    compile_available: bool
    findings_available: bool
    claims_available: bool
    evidence_available: bool
    ready_available: bool
    promote_available: bool


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    per_page: int


# ── record -> response conversions ──────────────────────────────────────────


def _to_draft_input(
    record: DraftInputRecord,
    active_parse_job_id: Optional[int],
    last_parse_job_id: Optional[int],
) -> DraftInput:
    return DraftInput(
        id=record.id,
        role=record.role,
        authority=record.authority,
        as_of_date=record.as_of_date,
        original_name=record.original_name,
        extension=record.extension,
        media_type=record.media_type,
        size_bytes=record.size_bytes,
        content_sha256=record.content_sha256,
        parse_status=record.parse_status,
        parse_error=record.parse_error,
        parsed_char_count=record.parsed_char_count,
        active_parse_job_id=active_parse_job_id,
        last_parse_job_id=last_parse_job_id,
        created_at=record.created_at,
    )


def _to_job(record: DraftJobRecord) -> DraftJob:
    return DraftJob(
        id=record.id,
        draft_id=record.draft_id,
        job_type=record.job_type,
        status=record.status,
        start_stage=record.start_stage,
        active_stage=record.active_stage,
        progress_percent=record.progress_percent,
        model_call_count=record.model_call_count,
        max_model_calls=record.max_model_calls,
        retry_count=record.retry_count,
        error_code=record.error_code,
        error_message=record.error_message,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


def _to_revision_summary(record: DraftRevisionRecord) -> DraftRevisionSummary:
    return DraftRevisionSummary(
        id=record.id,
        revision_no=record.revision_no,
        parent_revision_id=record.parent_revision_id,
        job_id=record.job_id,
        source=record.source,
        content_sha256=record.content_sha256,
        fact_status=record.fact_status,
        is_current=bool(record.is_current),
        created_by=record.created_by,
        created_at=record.created_at,
    )


def _to_revision_detail(record: DraftRevisionRecord) -> DraftRevisionDetail:
    return DraftRevisionDetail(
        summary=_to_revision_summary(record),
        content_md=record.content_md or "",
        sections=json.loads(record.sections_json or "[]"),
        citations=json.loads(record.citations_json or "[]"),
        qa_summary=json.loads(record.qa_summary_json or "{}"),
    )


def _sync_active_job(
    store: DraftStore, *, draft_id: int, owner_id: int
) -> Optional[DraftJobRecord]:
    """The draft's one active (pending/running) job, if any. Sync — call via ``to_thread``."""
    for job_status in ("running", "pending"):
        jobs, _total = store.list_jobs(
            draft_id=draft_id, owner_id=owner_id, status=job_status, page=1, per_page=1
        )
        if jobs:
            return jobs[0]
    return None


def _sync_summary_extras(store: DraftStore, draft: DraftRecord) -> dict[str, Any]:
    current = store.get_current_revision(draft_id=draft.id, owner_id=draft.created_by)
    inputs = store.list_inputs(draft_id=draft.id, owner_id=draft.created_by)
    active_job = _sync_active_job(store, draft_id=draft.id, owner_id=draft.created_by)
    return {
        "current_revision_id": current.id if current else None,
        "active_job_id": active_job.id if active_job else None,
        "input_count": len(inputs),
    }


def _sync_detail_extras(store: DraftStore, draft: DraftRecord) -> dict[str, Any]:
    inputs = store.list_inputs(draft_id=draft.id, owner_id=draft.created_by)
    input_rows = [
        (
            record,
            store.get_active_parse_job_id(record.id),
            store.get_last_parse_job_id(record.id),
        )
        for record in inputs
    ]
    current = store.get_current_revision(draft_id=draft.id, owner_id=draft.created_by)
    active_job = _sync_active_job(store, draft_id=draft.id, owner_id=draft.created_by)
    return {
        "inputs": input_rows,
        "current": current,
        "active_job": active_job,
        "revision_count": store.count_revisions(draft.id),
    }


async def _build_summary(
    store: DraftStore, draft: DraftRecord, evaluate: Callable, user: dict
) -> DraftSummary:
    extras = await asyncio.to_thread(_sync_summary_extras, store, draft)
    vault_access = await _vault_access_label(evaluate, user, draft.vault_id)
    return DraftSummary(
        id=draft.id,
        vault_id=draft.vault_id,
        vault_access=vault_access,
        title=draft.title,
        mode=draft.mode,
        status=draft.status,
        tier=draft.tier,
        lock_version=draft.lock_version,
        current_revision_id=extras["current_revision_id"],
        active_job_id=extras["active_job_id"],
        input_count=extras["input_count"],
        # Findings ship in a later PR (draft_findings does not exist yet).
        open_blocker_count=0,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        ready_at=draft.ready_at,
    )


async def _build_detail(
    store: DraftStore, draft: DraftRecord, evaluate: Callable, user: dict
) -> DraftDetail:
    extras = await asyncio.to_thread(_sync_detail_extras, store, draft)
    vault_access = await _vault_access_label(evaluate, user, draft.vault_id)
    current = extras["current"]
    active_job = extras["active_job"]
    summary = DraftSummary(
        id=draft.id,
        vault_id=draft.vault_id,
        vault_access=vault_access,
        title=draft.title,
        mode=draft.mode,
        status=draft.status,
        tier=draft.tier,
        lock_version=draft.lock_version,
        current_revision_id=current.id if current else None,
        active_job_id=active_job.id if active_job else None,
        input_count=len(extras["inputs"]),
        open_blocker_count=0,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        ready_at=draft.ready_at,
    )
    return DraftDetail(
        summary=summary,
        brief=json.loads(draft.brief_json or "{}"),
        inputs=[_to_draft_input(*row) for row in extras["inputs"]],
        current_revision_summary=_to_revision_summary(current) if current else None,
        # Compile ships in PR 2; no compile job can exist yet.
        active_compile_job=None,
        revision_count=extras["revision_count"],
        # draft_evidence/draft_claims/draft_findings ship in PR 2/PR 3.
        evidence_count=0,
        claim_counts_by_status={},
        finding_counts_by_severity={},
    )


def _validate_locked_spans(
    spans: list[LockedSpan], parsed_text: Optional[str]
) -> str:
    """Validate bounds/non-overlap/hash equality against the input's parsed text.

    Raises:
        DraftRoomHTTPError: 422 for out-of-bounds/overlapping spans (or spans
            supplied against an input with no parsed text yet), 409 when a
            span's hash no longer matches the current text (SPEC section 7).
    """
    if not spans:
        return "[]"
    if parsed_text is None:
        raise DraftRoomHTTPError(
            422,
            "input has no parsed text to validate locked spans against",
            "locked_spans_invalid",
        )
    text_len = len(parsed_text)
    ordered = sorted(spans, key=lambda s: s.start)
    prev_end = -1
    for span in ordered:
        if span.start < 0 or span.end > text_len:
            raise DraftRoomHTTPError(
                422, "locked span is out of bounds", "locked_spans_invalid"
            )
        if span.start < prev_end:
            raise DraftRoomHTTPError(
                422, "locked spans must not overlap", "locked_spans_invalid"
            )
        prev_end = span.end
        actual = sha256_text(parsed_text[span.start : span.end])
        if actual != span.sha256.lower():
            raise DraftRoomHTTPError(
                409,
                "locked span hash does not match current input text",
                "locked_spans_stale",
            )
    return json.dumps(
        [s.model_dump() for s in spans], sort_keys=True, separators=(",", ":")
    )


async def _delete_input_row_best_effort(
    store: DraftStore, *, draft_id: int, owner_id: int, input_id: int
) -> None:
    """Best-effort rollback of a reserved input row after a later upload step fails."""
    try:
        await asyncio.to_thread(
            store.delete_input_row,
            draft_id=draft_id,
            owner_id=owner_id,
            input_id=input_id,
        )
    except DraftStoreError:
        logger.error(
            "draft_room: rollback delete_input_row failed draft_id=%s input_id=%s",
            draft_id,
            input_id,
        )


def _export_filename(title: str, revision_no: int, tag: str) -> str:
    safe_title = secure_filename(title.replace(" ", "_")) or "draft"
    base = f"{safe_title}-rev{revision_no}"
    if tag:
        base = f"{base}-{tag}"
    return f"{base}.md"


# ── capabilities ─────────────────────────────────────────────────────────────


@router.get("/capabilities", response_model=DraftRoomCapabilities)
async def get_capabilities(
    user: dict = Depends(get_current_active_user),
) -> DraftRoomCapabilities:
    return DraftRoomCapabilities(
        enabled=settings.draft_room_enabled,
        modes=sorted(DRAFT_MODES),
        tiers=sorted(DRAFT_TIERS),
        piece_types=list(_PIECE_TYPES),
        transformation_strengths=list(_TRANSFORMATION_STRENGTHS),
        limits={
            "max_inputs": settings.draft_max_inputs,
            "max_total_input_mb": settings.draft_max_total_input_mb,
            "max_total_parsed_chars": settings.draft_max_total_parsed_chars,
            "parse_timeout_seconds": settings.draft_parse_timeout_seconds,
            "upload_rate_limit": settings.draft_upload_rate_limit,
        },
        export_formats=["md"],
        # This release ships project/input/job/revision CRUD only. These
        # gates are explicit so a client never has to guess.
        compile_available=False,
        findings_available=False,
        claims_available=False,
        evidence_available=False,
        ready_available=False,
        promote_available=False,
    )


# ── drafts ───────────────────────────────────────────────────────────────────


@router.post("/drafts", status_code=201, response_model=DraftSummary)
async def create_draft(
    body: DraftCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftSummary:
    _require_enabled()
    await _require_vault_read(evaluate, user, body.vault_id)
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(
        lambda: store.create_draft(
            vault_id=body.vault_id,
            created_by=owner_id,
            title=body.title,
            mode=body.mode,
            tier=body.tier,
            brief_json=json.dumps(
                body.brief.model_dump(), sort_keys=True, separators=(",", ":")
            ),
        )
    )
    return await _build_summary(store, draft, evaluate, user)


@router.get("/drafts", response_model=PaginatedResponse[DraftSummary])
async def list_drafts(
    vault_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> PaginatedResponse[DraftSummary]:
    owner_id = int(user["id"])
    store = DraftStore(db)
    drafts, total = await _run_store(
        lambda: store.list_drafts(
            owner_id=owner_id, vault_id=vault_id, status=status, page=page, per_page=per_page
        )
    )
    # Rows whose vault access was revoked are intentionally included (never
    # suppressed) so the owner can still find and delete them (SPEC 9.1).
    items = [await _build_summary(store, d, evaluate, user) for d in drafts]
    return PaginatedResponse[DraftSummary](items=items, total=total, page=page, per_page=per_page)


@router.get("/drafts/{draft_id}", response_model=DraftDetail)
async def get_draft_detail(
    draft_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> DraftDetail:
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    return await _build_detail(store, draft, evaluate, user)


@router.patch("/drafts/{draft_id}", response_model=DraftSummary)
async def update_draft(
    draft_id: int,
    body: DraftUpdateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftSummary:
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    brief_json = None
    if body.brief is not None:
        brief_json = json.dumps(
            body.brief.model_dump(), sort_keys=True, separators=(",", ":")
        )
    updated = await _run_store(
        lambda: store.update_draft(
            draft_id=draft_id,
            owner_id=owner_id,
            lock_version=body.lock_version,
            title=body.title,
            brief_json=brief_json,
            tier=body.tier,
        )
    )
    return await _build_summary(store, updated, evaluate, user)


@router.post("/drafts/{draft_id}/archive", response_model=DraftSummary)
async def archive_draft(
    draft_id: int,
    body: LockVersionRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftSummary:
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    updated = await _run_store(
        lambda: store.archive_draft(
            draft_id=draft_id, owner_id=owner_id, lock_version=body.lock_version
        )
    )
    return await _build_summary(store, updated, evaluate, user)


@router.post("/drafts/{draft_id}/restore", response_model=DraftSummary)
async def restore_draft(
    draft_id: int,
    body: LockVersionRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftSummary:
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    updated = await _run_store(
        lambda: store.restore_draft(
            draft_id=draft_id, owner_id=owner_id, lock_version=body.lock_version
        )
    )
    return await _build_summary(store, updated, evaluate, user)


@router.delete("/drafts/{draft_id}", status_code=204)
async def delete_draft(
    draft_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    storage: DraftInputStorage = Depends(get_draft_input_storage),
    _csrf_token: str = Depends(csrf_protect),
) -> None:
    """Whole-draft purge. Owner-only — allowed even after vault access loss
    so a user who lost access can still remove their private data."""
    owner_id = int(user["id"])
    store = DraftStore(db)
    # Ownership check first: a draft owned by someone else is 404, never 403.
    await _run_store(lambda: store.get_draft(draft_id, owner_id))
    deletion = DraftDeletionService(storage)
    try:
        await asyncio.to_thread(
            deletion.delete_draft, store, draft_id=draft_id, owner_id=owner_id
        )
    except DraftStoreError as exc:
        raise _map_store_error(exc) from exc
    # Recorded through the global HMAC audit log (not draft_events, which
    # cascade-deletes) only once deletion has actually succeeded — writing
    # it earlier would leave a signed, tamper-evident row asserting a
    # deletion that a guard check (e.g. DraftConflictError code='active_job'
    # -> 409) rejected. `security_audit_log` has no foreign key on drafts
    # (app/models/database.py) and this event's metadata is just the
    # `draft_id`, so nothing about the record depends on the draft's rows
    # still existing — recording after the cascade loses no information
    # while guaranteeing the record only ever reflects a real deletion
    # (SPEC section 9.3).
    await safe_record_security_event(
        db,
        event_type="draft_deleted",
        actor=user,
        request=request,
        metadata={"draft_id": draft_id},
    )


# ── inputs ───────────────────────────────────────────────────────────────────


@router.post("/drafts/{draft_id}/inputs", status_code=202, response_model=DraftInputUploadResponse)
@limiter.limit(settings.draft_upload_rate_limit)
async def upload_draft_input(
    request: Request,
    draft_id: int,
    role: str = Form(...),
    authority: str = Form("unknown"),
    as_of_date: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    storage: DraftInputStorage = Depends(get_draft_input_storage),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftInputUploadResponse:
    _require_enabled()
    if role not in INPUT_ROLES:
        raise DraftRoomHTTPError(
            422, f"role must be one of {sorted(INPUT_ROLES)}", "validation_failed"
        )
    if authority not in INPUT_AUTHORITIES:
        raise DraftRoomHTTPError(
            422, f"authority must be one of {sorted(INPUT_AUTHORITIES)}", "validation_failed"
        )
    if file is None or not file.filename:
        raise DraftRoomHTTPError(400, "file is required", "missing_file")

    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)

    # Cheap pre-check on count before streaming bytes (SPEC section 6.2); the
    # byte-total limit is re-checked atomically inside reserve_input once the
    # upload's real size is known.
    existing_inputs = await _run_store(
        lambda: store.list_inputs(draft_id=draft_id, owner_id=owner_id)
    )
    if len(existing_inputs) >= settings.draft_max_inputs:
        raise DraftRoomHTTPError(
            413,
            f"draft already holds the maximum of {settings.draft_max_inputs} inputs",
            "limit_exceeded",
        )

    try:
        staged = await storage.stage_upload(
            file,
            allowed_extensions=settings.allowed_extensions,
            max_file_bytes=settings.max_file_size_mb * 1024 * 1024,
        )
    except DraftInputStorageError as exc:
        raise _map_storage_error(exc) from exc

    try:
        input_record = await _run_store(
            lambda: store.reserve_input(
                draft_id=draft_id,
                owner_id=owner_id,
                role=role,
                authority=authority,
                as_of_date=as_of_date,
                original_name=staged.original_name,
                stored_name=staged.stored_name,
                extension=staged.extension,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                max_inputs=settings.draft_max_inputs,
                max_total_input_bytes=settings.draft_max_total_input_mb * 1024 * 1024,
            )
        )
    except BaseException:
        # Any failure here — not just the translated DraftRoomHTTPError —
        # must discard the staged `.part` file (SPEC section 6.1: "Delete
        # partial files on any error"). `_run_store` only translates
        # DraftStoreError; a raw sqlite3.OperationalError (e.g. "database is
        # locked") would otherwise propagate untranslated and strand the
        # staged bytes until the 24h startup reconcile.
        await asyncio.to_thread(storage.discard, staged)
        raise

    try:
        await asyncio.to_thread(storage.finalize, staged, input_record.storage_relpath)
    except Exception as exc:
        await asyncio.to_thread(storage.discard, staged)
        await _delete_input_row_best_effort(
            store, draft_id=draft_id, owner_id=owner_id, input_id=input_record.id
        )
        logger.error(
            "draft_room: failed to finalize staged upload draft_id=%s input_id=%s",
            draft_id,
            input_record.id,
        )
        raise DraftRoomHTTPError(500, "failed to store uploaded file", "internal_error") from exc

    try:
        job = await _run_store(
            lambda: store.enqueue_parse_job(
                draft_id=draft_id,
                owner_id=owner_id,
                input_id=input_record.id,
                timeout_seconds=settings.draft_parse_timeout_seconds,
            )
        )
    except DraftRoomHTTPError:
        await asyncio.to_thread(
            lambda: storage.resolve(input_record.storage_relpath).unlink(missing_ok=True)
        )
        await _delete_input_row_best_effort(
            store, draft_id=draft_id, owner_id=owner_id, input_id=input_record.id
        )
        raise

    return DraftInputUploadResponse(
        input=_to_draft_input(input_record, job.id, job.id),
        job=_to_job(job),
    )


@router.patch("/drafts/{draft_id}/inputs/{input_id}", response_model=DraftInput)
async def update_draft_input(
    draft_id: int,
    input_id: int,
    body: InputUpdateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftInput:
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)

    locked_spans_json = None
    if body.locked_spans is not None:
        parsed_text = await _run_store(
            lambda: store.get_input_parsed_text(
                draft_id=draft_id, owner_id=owner_id, input_id=input_id
            )
        )
        locked_spans_json = _validate_locked_spans(body.locked_spans, parsed_text)

    updated = await _run_store(
        lambda: store.update_input_metadata(
            draft_id=draft_id,
            owner_id=owner_id,
            input_id=input_id,
            role=body.role,
            authority=body.authority,
            as_of_date=body.as_of_date,
            clear_as_of_date=body.clear_as_of_date,
            locked_spans_json=locked_spans_json,
        )
    )
    active_job_id, last_job_id = await asyncio.to_thread(
        lambda: (
            store.get_active_parse_job_id(input_id),
            store.get_last_parse_job_id(input_id),
        )
    )
    return _to_draft_input(updated, active_job_id, last_job_id)


@router.get(
    "/drafts/{draft_id}/inputs/{input_id}/content", response_model=DraftInputContent
)
async def get_draft_input_content(
    draft_id: int,
    input_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> DraftInputContent:
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    record = await _run_store(
        lambda: store.get_input(draft_id=draft_id, owner_id=owner_id, input_id=input_id)
    )
    parsed_text = await _run_store(
        lambda: store.get_input_parsed_text(
            draft_id=draft_id, owner_id=owner_id, input_id=input_id
        )
    )
    return DraftInputContent(
        input_id=input_id, parse_status=record.parse_status, parsed_text=parsed_text
    )


@router.delete("/drafts/{draft_id}/inputs/{input_id}", status_code=204)
async def delete_draft_input(
    draft_id: int,
    input_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    storage: DraftInputStorage = Depends(get_draft_input_storage),
    _csrf_token: str = Depends(csrf_protect),
) -> None:
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    deletion = DraftDeletionService(storage)
    try:
        await asyncio.to_thread(
            deletion.delete_input,
            store,
            draft_id=draft_id,
            owner_id=owner_id,
            input_id=input_id,
        )
    except DraftStoreError as exc:
        raise _map_store_error(exc) from exc
    except DraftInputStorageError as exc:
        raise _map_storage_error(exc) from exc


# ── jobs ─────────────────────────────────────────────────────────────────────


@router.get("/drafts/{draft_id}/jobs", response_model=PaginatedResponse[DraftJob])
async def list_draft_jobs(
    draft_id: int,
    job_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> PaginatedResponse[DraftJob]:
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    jobs, total = await _run_store(
        lambda: store.list_jobs(
            draft_id=draft_id,
            owner_id=owner_id,
            job_type=job_type,
            status=status,
            page=page,
            per_page=per_page,
        )
    )
    return PaginatedResponse[DraftJob](
        items=[_to_job(j) for j in jobs], total=total, page=page, per_page=per_page
    )


@router.get("/drafts/{draft_id}/jobs/{job_id}", response_model=DraftJob)
async def get_draft_job(
    draft_id: int,
    job_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> DraftJob:
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    job = await _run_store(
        lambda: store.get_job(draft_id=draft_id, owner_id=owner_id, job_id=job_id)
    )
    return _to_job(job)


@router.post("/drafts/{draft_id}/jobs/{job_id}/cancel", response_model=DraftJob)
async def cancel_draft_job(
    draft_id: int,
    job_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftJob:
    """Owner-only — allowed even after vault access loss (SPEC 9.1: cancelling
    only reduces private processing, it does not expose vault-derived content)."""
    owner_id = int(user["id"])
    store = DraftStore(db)
    job = await _run_store(
        lambda: store.request_job_cancel(
            draft_id=draft_id, owner_id=owner_id, job_id=job_id
        )
    )
    return _to_job(job)


@router.post("/drafts/{draft_id}/jobs/{job_id}/retry", status_code=202, response_model=DraftJob)
async def retry_draft_job(
    draft_id: int,
    job_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftJob:
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    job = await _run_store(
        lambda: store.retry_parse_job(
            draft_id=draft_id,
            owner_id=owner_id,
            job_id=job_id,
            timeout_seconds=settings.draft_parse_timeout_seconds,
        )
    )
    return _to_job(job)


# ── SSE events ───────────────────────────────────────────────────────────────


@router.get("/drafts/{draft_id}/events")
async def draft_room_events_stream(request: Request, draft_id: int) -> StreamingResponse:
    """Authenticated notification stream (SPEC section 8.4).

    Auth and the vault read-permission check run against a short-lived pooled
    connection acquired directly from ``request.app.state.db_pool`` and
    released BEFORE the ``StreamingResponse`` begins — mirroring
    ``wiki.py``'s ``GET /api/wiki/events`` (issue #301 connection-lifecycle
    pattern). ``Depends(get_db)`` is deliberately not used here: FastAPI only
    runs its teardown after the whole stream completes, which would pin a
    pool slot for the connection's entire lifetime.
    """
    pool = request.app.state.db_pool
    with pool.connection() as conn:
        user = await _resolve_active_user(
            conn,
            request,
            request.headers.get("authorization"),
            request.cookies.get("access_token"),
        )
        owner_id = int(user["id"])
        store = DraftStore(conn)
        try:
            draft = store.get_draft(draft_id, owner_id)
        except DraftNotFoundError:
            raise DraftRoomHTTPError(404, "draft not found", "not_found")
        if not await _evaluate_policy(conn, user, "vault", draft.vault_id, "read"):
            raise DraftRoomHTTPError(
                403, "no read access to this vault", "vault_access_revoked"
            )
        active_job = _sync_active_job(store, draft_id=draft_id, owner_id=owner_id)
        vault_id = draft.vault_id
    # <-- pooled connection released here, before the SSE stream starts.
    log_db_released("draft_room_events_stream", vault_id=vault_id)

    bus = get_draft_event_bus()
    queue = bus.subscribe(draft_id)

    async def event_generator():
        try:
            subscribed = build_event(
                "subscribed",
                draft_id=draft_id,
                job_id=active_job.id if active_job else None,
                status=active_job.status if active_job else None,
            )
            yield f"data: {json.dumps(subscribed)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe(draft_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── revisions ────────────────────────────────────────────────────────────────


@router.get(
    "/drafts/{draft_id}/revisions", response_model=PaginatedResponse[DraftRevisionSummary]
)
async def list_draft_revisions(
    draft_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> PaginatedResponse[DraftRevisionSummary]:
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    revisions, total = await _run_store(
        lambda: store.list_revisions(
            draft_id=draft_id, owner_id=owner_id, page=page, per_page=per_page
        )
    )
    return PaginatedResponse[DraftRevisionSummary](
        items=[_to_revision_summary(r) for r in revisions],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/drafts/{draft_id}/revisions/{revision_id}", response_model=DraftRevisionDetail)
async def get_draft_revision(
    draft_id: int,
    revision_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> DraftRevisionDetail:
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    revision = await _run_store(
        lambda: store.get_revision(
            draft_id=draft_id, owner_id=owner_id, revision_id=revision_id, include_content=True
        )
    )
    return _to_revision_detail(revision)


@router.post("/drafts/{draft_id}/revisions", status_code=201, response_model=DraftRevisionDetail)
async def create_draft_revision(
    draft_id: int,
    body: RevisionCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftRevisionDetail:
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    revision = await _run_store(
        lambda: store.create_manual_revision(
            draft_id=draft_id,
            owner_id=owner_id,
            lock_version=body.lock_version,
            base_revision_id=body.base_revision_id,
            content_md=body.content_md,
        )
    )
    return _to_revision_detail(revision)


@router.post("/drafts/{draft_id}/revisions/{revision_id}/export")
async def export_draft_revision(
    draft_id: int,
    revision_id: int,
    format: str = Query("md"),
    acknowledge_not_fact_checked: bool = Query(False),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> Response:
    """Return the exact stored revision bytes with fact/approval-status headers.

    ``fact_status`` is always ``'not_run'`` for manual revisions in this
    release (no Fact Desk yet), so export requires an explicit
    acknowledgement and uses a ``-UNVERIFIED.md`` filename. The Markdown body
    itself is never mutated.
    """
    if format != "md":
        raise DraftRoomHTTPError(422, "unsupported export format", "unsupported_export_format")
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    revision = await _run_store(
        lambda: store.get_revision(
            draft_id=draft_id, owner_id=owner_id, revision_id=revision_id, include_content=True
        )
    )

    fact_status = revision.fact_status
    is_ready = draft.ready_revision_id == revision.id
    if is_ready and fact_status == "passed":
        tag, approval_status = "", "ready"
    elif fact_status == "passed":
        tag, approval_status = "REVIEW", "not_ready"
    else:
        if not acknowledge_not_fact_checked:
            raise DraftRoomHTTPError(
                422,
                "export requires acknowledge_not_fact_checked=true because "
                "this revision has not passed fact checking",
                "export_ack_required",
            )
        tag = "UNVERIFIED"
        approval_status = "ready" if is_ready else "not_ready"

    filename = _export_filename(draft.title, revision.revision_no, tag)
    await _run_store(
        lambda: store.record_event(
            draft_id=draft_id,
            owner_id=owner_id,
            event_type="exported",
            actor_user_id=owner_id,
            revision_id=revision.id,
            payload={"format": "md", "fact_status": fact_status},
        )
    )
    return Response(
        content=revision.content_md or "",
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Draft-Fact-Status": fact_status,
            "X-Draft-Approval-Status": approval_status,
            "X-Draft-Content-Sha256": revision.content_sha256,
        },
    )
