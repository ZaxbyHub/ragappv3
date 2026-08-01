"""Draft Room HTTP API (issues #435/#436, ``specs/draft-room/SPEC.md`` sections 7-9).

Private, owner-scoped drafting projects: project/brief CRUD, raw-input upload
and metadata (never `files`/LanceDB/Wiki/KMS), the durable parse-job lifecycle,
the compile pipeline (enqueue/stage history/cancel/retry), the evidence, claim
and finding ledgers with human finding disposition, the human-only Ready
transition, manual Markdown revisions, Markdown export, and an authenticated
SSE notification stream.

Promotion (``POST /drafts/{id}/promote``) is deliberately NOT implemented — it
is issue #437 / SPEC PR 4. ``GET /capabilities`` advertises it as unavailable
rather than leaving a future client to guess.

Compile-surface invariants enforced here (SPEC sections 8.2, 9.1, 12.5):

* ``Idempotency-Key`` is ASCII 1-128 characters, scoped to the authenticated
  user plus a canonical request fingerprint. Same key + same fingerprint
  returns the existing job; same key + a different fingerprint is 409.
* Large bodies (stage artifacts, evidence passages, claim ledgers) are paged
  or explicitly fetched — never embedded in a summary response.
* Users may never select the ``assemble`` start stage: the accepted enum is
  derived from :data:`~app.services.draft_pipeline.COMPILE_STAGE_ORDER` with
  ``intake`` and ``assemble`` removed, and every accepted start stage still
  runs through Fact and Assemble because the orchestrator always walks the
  whole tuple.
* Applying a finding creates an immutable manual revision under span/hash
  optimistic concurrency (a stale span is 409, never a silent overwrite),
  invalidates Fact/Ready, and never happens without a user actor.
* Ready is a human action only. Nothing in this module — and nothing in
  ``draft_pipeline`` — can reach ``status='ready'`` except
  :func:`mark_revision_ready`, which is reachable solely from the owner's
  authenticated request.
* No response field is ever named ``confidence``, ``support``,
  ``correctness``, ``entailment``, ``verification`` or
  ``support_probability``. The only score name is ``lexical_overlap_score``
  (SPEC section 12.3).

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
from app.services.draft_pipeline import COMPILE_STAGE_ORDER
from app.services.draft_prompts import PROMPT_BUNDLE_VERSION
from app.services.draft_provider_policy import (
    ProviderPolicyError,
    assert_provider_allowed,
)
from app.services.draft_store import (
    CLAIM_STATUSES,
    DRAFT_MODES,
    DRAFT_TIERS,
    FINDING_SEVERITIES,
    FINDING_STATUSES,
    INPUT_AUTHORITIES,
    INPUT_ROLES,
    DraftClaimRecord,
    DraftClaimSourceRecord,
    DraftConflictError,
    DraftEvidenceRecord,
    DraftFindingRecord,
    DraftInputRecord,
    DraftJobRecord,
    DraftNotFoundError,
    DraftRecord,
    DraftRevisionRecord,
    DraftStageRecord,
    DraftStore,
    DraftStoreError,
    canonical_json,
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

# ── compile-surface constants (SPEC sections 8.1, 8.2, 12.5) ────────────────

#: Stages a *user* may name as a compile/retry start point. Derived from the
#: orchestrator's canonical order rather than re-typed, so the two can never
#: drift: ``intake`` is machine-only bookkeeping and ``assemble`` must NEVER be
#: user-selectable (SPEC section 8.1 — "it never accepts or exposes `assemble`
#: as a user-selected start"). Every stage listed here still continues through
#: Fact and Assemble, because ``_CompileRun.execute`` walks the whole of
#: ``COMPILE_STAGE_ORDER`` regardless of where the run is resumed.
_MACHINE_ONLY_STAGES: frozenset[str] = frozenset({"intake", "assemble"})
_START_STAGES: tuple[str, ...] = tuple(
    stage for stage in COMPILE_STAGE_ORDER if stage not in _MACHINE_ONLY_STAGES
)
_DEFAULT_START_STAGE = "research"

#: An Assemble failure normalizes to ``fact`` on retry (SPEC section 8.1).
_RETRY_STAGE_NORMALIZATION: dict[str, str] = {"assemble": "fact", "intake": "research"}

#: Draft statuses a compile may legally start from. Kept as an explicit
#: allowlist because ``DraftStore``'s transition tables are module-private; a
#: Ready draft is first invalidated to ``needs_review`` inside the same
#: transaction, so the write always lands on ``queued`` from a legal prior.
_COMPILE_ALLOWED_PRIOR_STATUSES: frozenset[str] = frozenset(
    {"draft", "needs_review", "failed", "cancelled", "ready"}
)

#: Claim verdicts that are non-waivable Ready blockers (SPEC section 12.5
#: rule 3). Mirrors ``draft_pipeline._BLOCKING_CLAIM_STATUSES``.
_BLOCKING_CLAIM_STATUSES: frozenset[str] = frozenset(
    {"contradicted", "unsupported", "ambiguous", "stale"}
)

#: Revision fact states in which a Fact result currently describes the text
#: (SPEC section 8.2 export rules). Anything else is ``not_run``/``running``/
#: ``invalidated`` and needs an explicit acknowledgement to export.
_FACT_CURRENT_STATUSES: frozenset[str] = frozenset({"passed", "findings"})

#: Upper bound on rows scanned when a listing needs an in-Python filter the
#: store's paged accessors do not express (claim ``status``, finding
#: ``status``/``severity``). Reads are still issued through the store's
#: ``limit``/``offset`` methods in ``_LIST_SCAN_CHUNK``-sized pages; this only
#: bounds how deep the scan goes so a pathological ledger cannot be pulled
#: into memory. A single revision's ledger is bounded far below this by
#: ``draft_max_sections`` and the pipeline's per-stage finding caps.
_LIST_SCAN_CHUNK = 200
_MAX_LIST_SCAN_ROWS = 2000

#: Bounded number of evidence citations attached to one returned claim.
_MAX_CLAIM_SOURCES = 50

_IDEMPOTENCY_HEADER = "Idempotency-Key"

#: ``draft_events.event_type`` written for each human finding disposition
#: (SPEC section 5.8 requires at minimum ``finding_waived``).
_DISPOSITION_EVENT_TYPES: dict[str, str] = {
    "apply": "finding_applied",
    "dismiss": "finding_dismissed",
    "waive": "finding_waived",
}


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


def _validate_start_stage(value: Optional[str]) -> Optional[str]:
    """Reject any stage a user may not select — ``assemble`` above all."""
    if value is None:
        return None
    if value not in _START_STAGES:
        raise ValueError(f"start_stage must be one of {list(_START_STAGES)}")
    return value


class CompileRequest(BaseModel):
    """SPEC section 8.1. ``base_revision_id`` is required but may be null, and
    is null only when the draft has no current revision."""

    base_revision_id: Optional[int] = Field(...)
    lock_version: int
    start_stage: str = _DEFAULT_START_STAGE

    @field_validator("start_stage")
    @classmethod
    def _check_start_stage(cls, v: str) -> str:
        return _validate_start_stage(v)  # type: ignore[return-value]


class RetryJobRequest(BaseModel):
    """SPEC section 8.1. Omitting ``start_stage`` restarts at the failed or
    incomplete stage; an Assemble failure normalizes to ``fact``."""

    start_stage: Optional[str] = None

    @field_validator("start_stage")
    @classmethod
    def _check_start_stage(cls, v: Optional[str]) -> Optional[str]:
        return _validate_start_stage(v)


class FindingDispositionRequest(BaseModel):
    """SPEC section 8.2. ``note`` is the user's own reason; it is required and
    must be non-empty for ``waive``."""

    action: str
    base_revision_id: Optional[int] = Field(...)
    lock_version: int
    note: Optional[str] = Field(None, max_length=2000)

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        if v not in ("apply", "dismiss", "waive"):
            raise ValueError("action must be one of ['apply', 'dismiss', 'waive']")
        return v


class ReadyRequest(BaseModel):
    lock_version: int
    acknowledge_source_only: bool = False


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


class DraftStage(BaseModel):
    """One immutable compile-stage attempt (SPEC section 8.1).

    ``content_md`` is populated only when the caller passes
    ``include_content=true``; ``artifact`` is the stage's validated structured
    artifact, decoded from its stored canonical JSON.
    """

    id: int
    job_id: int
    stage: str
    attempt: int
    status: str
    input_sha256: str
    artifact_sha256: Optional[str]
    candidate_sha256: Optional[str]
    semantic_changed: bool
    prompt_id: Optional[str]
    prompt_version: Optional[str]
    prompt_sha256: Optional[str]
    model_name: Optional[str]
    temperature: Optional[float]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    error_code: Optional[str]
    error_message: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    artifact: Any
    content_md: Optional[str] = None


class DraftEvidence(BaseModel):
    """A retrieval-time evidence snapshot. Carries display metadata and the
    snapshotted passage only — never a storage path or a secret."""

    id: int
    job_id: int
    label: str
    source_kind: str
    title: str
    passage: str
    passage_sha256: str
    source_content_sha256: str
    draft_input_id: Optional[int]
    file_id: Optional[int]
    wiki_page_id: Optional[int]
    wiki_claim_id: Optional[int]
    kms_entry_id: Optional[int]
    chunk_uid: Optional[str]
    page_number: Optional[int]
    section: Optional[str]
    retrieval_score: Optional[float]
    authority: str
    as_of_date: Optional[str]
    source_updated_at: Optional[str]
    source_deleted_at: Optional[str]
    source_deleted: bool


class DraftClaimSource(BaseModel):
    """One evidence citation on a claim.

    ``lexical_overlap_score`` is a per-citation lexical diagnostic. SPEC
    section 12.3 forbids presenting it as claim confidence, factual
    confidence, support probability, verification, or entailment, and this is
    the ONLY score name on the Draft Room wire.
    """

    id: int
    claim_id: int
    evidence_id: int
    relationship: str
    exact_quote: str
    passage_start: Optional[int]
    passage_end: Optional[int]
    lexical_overlap_score: Optional[float]


class DraftClaim(BaseModel):
    id: int
    revision_id: int
    ordinal: int
    claim_text: str
    claim_sha256: str
    span_start: int
    span_end: int
    claim_type: str
    status: str
    severity: str
    rationale: str
    retrieval_audit: Any
    resolution: str
    resolved_by: Optional[int]
    resolved_at: Optional[str]
    resolution_note: Optional[str]
    sources: list[DraftClaimSource]


class DraftFinding(BaseModel):
    """A finding plus its disposition eligibility (SPEC section 8.2).

    ``can_dismiss`` is False for every blocker — blockers may only be applied
    or validly waived. ``can_waive`` additionally requires ``waivable``.
    """

    id: int
    draft_id: int
    revision_id: Optional[int]
    job_id: Optional[int]
    stage: str
    rule_id: str
    rule_version: str
    category: str
    severity: str
    status: str
    waivable: bool
    message: str
    original_text: Optional[str]
    suggestion: Optional[str]
    span_start: Optional[int]
    span_end: Optional[int]
    span_text_sha256: Optional[str]
    resolved_by: Optional[int]
    resolved_at: Optional[str]
    resolution_note: Optional[str]
    waiver_rule_version: Optional[str]
    waiver_text_sha256: Optional[str]
    created_at: str
    can_apply: bool
    can_dismiss: bool
    can_waive: bool


class FindingDispositionResponse(BaseModel):
    finding: DraftFinding
    revision: Optional[DraftRevisionSummary]


class DraftRoomCapabilities(BaseModel):
    enabled: bool
    modes: list[str]
    tiers: list[str]
    piece_types: list[str]
    transformation_strengths: list[str]
    limits: dict[str, Any]
    export_formats: list[str]
    logical_model_modes: list[str]
    default_logical_mode: str
    compile_start_stages: list[str]
    compile_stage_order: list[str]
    prompt_bundle_version: str
    editorial_gates_installed: bool
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


def _json_or(raw: Optional[str], default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _to_stage(record: DraftStageRecord, *, include_content: bool) -> DraftStage:
    return DraftStage(
        id=record.id,
        job_id=record.job_id,
        stage=record.stage,
        attempt=record.attempt,
        status=record.status,
        input_sha256=record.input_sha256,
        artifact_sha256=record.artifact_sha256,
        candidate_sha256=record.candidate_sha256,
        semantic_changed=bool(record.semantic_changed),
        prompt_id=record.prompt_id,
        prompt_version=record.prompt_version,
        prompt_sha256=record.prompt_sha256,
        model_name=record.model_name,
        temperature=record.temperature,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        error_code=record.error_code,
        error_message=record.error_message,
        started_at=record.started_at,
        completed_at=record.completed_at,
        artifact=_json_or(record.artifact_json, {}),
        content_md=record.content_md if include_content else None,
    )


def _to_evidence(record: DraftEvidenceRecord) -> DraftEvidence:
    return DraftEvidence(
        id=record.id,
        job_id=record.job_id,
        label=record.label,
        source_kind=record.source_kind,
        title=record.title,
        passage=record.passage,
        passage_sha256=record.passage_sha256,
        source_content_sha256=record.source_content_sha256,
        draft_input_id=record.draft_input_id,
        file_id=record.file_id,
        wiki_page_id=record.wiki_page_id,
        wiki_claim_id=record.wiki_claim_id,
        kms_entry_id=record.kms_entry_id,
        chunk_uid=record.chunk_uid,
        page_number=record.page_number,
        section=record.section,
        retrieval_score=record.retrieval_score,
        authority=record.authority,
        as_of_date=record.as_of_date,
        source_updated_at=record.source_updated_at,
        source_deleted_at=record.source_deleted_at,
        source_deleted=record.source_deleted_at is not None,
    )


def _to_claim_source(record: DraftClaimSourceRecord) -> DraftClaimSource:
    return DraftClaimSource(
        id=record.id,
        claim_id=record.claim_id,
        evidence_id=record.evidence_id,
        relationship=record.relationship,
        exact_quote=record.exact_quote,
        passage_start=record.passage_start,
        passage_end=record.passage_end,
        lexical_overlap_score=record.lexical_overlap_score,
    )


def _to_claim(
    record: DraftClaimRecord, sources: list[DraftClaimSourceRecord]
) -> DraftClaim:
    return DraftClaim(
        id=record.id,
        revision_id=record.revision_id,
        ordinal=record.ordinal,
        claim_text=record.claim_text,
        claim_sha256=record.claim_sha256,
        span_start=record.span_start,
        span_end=record.span_end,
        claim_type=record.claim_type,
        status=record.status,
        severity=record.severity,
        rationale=record.rationale,
        retrieval_audit=_json_or(record.retrieval_audit_json, {}),
        resolution=record.resolution,
        resolved_by=record.resolved_by,
        resolved_at=record.resolved_at,
        resolution_note=record.resolution_note,
        sources=[_to_claim_source(s) for s in sources],
    )


def _to_finding(record: DraftFindingRecord) -> DraftFinding:
    is_open = record.status == "open"
    is_blocker = record.severity == "blocker"
    return DraftFinding(
        id=record.id,
        draft_id=record.draft_id,
        revision_id=record.revision_id,
        job_id=record.job_id,
        stage=record.stage,
        rule_id=record.rule_id,
        rule_version=record.rule_version,
        category=record.category,
        severity=record.severity,
        status=record.status,
        waivable=bool(record.waivable),
        message=record.message,
        original_text=record.original_text,
        suggestion=record.suggestion,
        span_start=record.span_start,
        span_end=record.span_end,
        span_text_sha256=record.span_text_sha256,
        resolved_by=record.resolved_by,
        resolved_at=record.resolved_at,
        resolution_note=record.resolution_note,
        waiver_rule_version=record.waiver_rule_version,
        waiver_text_sha256=record.waiver_text_sha256,
        created_at=record.created_at,
        can_apply=bool(
            is_open
            and record.suggestion is not None
            and record.span_start is not None
            and record.span_end is not None
        ),
        # SPEC section 8.2: dismiss is allowed only for non-blockers.
        can_dismiss=bool(is_open and not is_blocker),
        can_waive=bool(is_open and record.waivable),
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


def _sync_active_compile_job(
    store: DraftStore, *, draft_id: int, owner_id: int
) -> Optional[DraftJobRecord]:
    """The draft's one active ``compile`` job, if any. Sync — via ``to_thread``."""
    for job_status in ("running", "pending"):
        jobs, _total = store.list_jobs(
            draft_id=draft_id,
            owner_id=owner_id,
            job_type="compile",
            status=job_status,
            page=1,
            per_page=1,
        )
        if jobs:
            return jobs[0]
    return None


# ── scoped counters ──────────────────────────────────────────────────────────
#
# Every counter below constrains through the owning draft (SPEC section 9.1
# rule 5) — a child ID is never globally sufficient authorization. They exist
# because ``DraftStore`` ships paged list accessors but no COUNT accessors, and
# a listing contract of ``{items, total, page, per_page}`` needs a real total
# that does not require materializing the rows.


def _sync_open_blocker_count(conn: sqlite3.Connection, draft_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM draft_findings "
            "WHERE draft_id = ? AND severity = 'blocker' AND status = 'open'",
            (draft_id,),
        ).fetchone()[0]
    )


def _sync_count_stages(conn: sqlite3.Connection, *, job_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM draft_job_stages WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    )


def _sync_count_evidence(conn: sqlite3.Connection, *, job_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM draft_evidence WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    )


def _sync_ledger_counts(
    conn: sqlite3.Connection, *, draft_id: int, current_revision_id: Optional[int]
) -> dict[str, Any]:
    """Evidence/claim/finding roll-ups for ``GET /drafts/{id}`` detail."""
    finding_counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT severity, COUNT(*) FROM draft_findings "
        "WHERE draft_id = ? AND status = 'open' GROUP BY severity",
        (draft_id,),
    ).fetchall():
        finding_counts[str(row[0])] = int(row[1])

    claim_counts: dict[str, int] = {}
    evidence_count = 0
    if current_revision_id is not None:
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM draft_claims WHERE revision_id = ? "
            "GROUP BY status",
            (current_revision_id,),
        ).fetchall():
            claim_counts[str(row[0])] = int(row[1])
        evidence_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM draft_evidence e "
                "WHERE e.job_id = (SELECT job_id FROM draft_revisions WHERE id = ?)",
                (current_revision_id,),
            ).fetchone()[0]
        )
    return {
        "evidence_count": evidence_count,
        "claim_counts_by_status": claim_counts,
        "finding_counts_by_severity": finding_counts,
    }


def _sync_summary_extras(
    conn: sqlite3.Connection, store: DraftStore, draft: DraftRecord
) -> dict[str, Any]:
    current = store.get_current_revision(draft_id=draft.id, owner_id=draft.created_by)
    inputs = store.list_inputs(draft_id=draft.id, owner_id=draft.created_by)
    active_job = _sync_active_job(store, draft_id=draft.id, owner_id=draft.created_by)
    return {
        "current_revision_id": current.id if current else None,
        "active_job_id": active_job.id if active_job else None,
        "input_count": len(inputs),
        "open_blocker_count": _sync_open_blocker_count(conn, draft.id),
    }


def _sync_detail_extras(
    conn: sqlite3.Connection, store: DraftStore, draft: DraftRecord
) -> dict[str, Any]:
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
    active_compile = _sync_active_compile_job(
        store, draft_id=draft.id, owner_id=draft.created_by
    )
    return {
        "inputs": input_rows,
        "current": current,
        "active_job": active_job,
        "active_compile_job": active_compile,
        "revision_count": store.count_revisions(draft.id),
        "open_blocker_count": _sync_open_blocker_count(conn, draft.id),
        "ledger": _sync_ledger_counts(
            conn,
            draft_id=draft.id,
            current_revision_id=current.id if current else None,
        ),
    }


async def _build_summary(
    conn: sqlite3.Connection,
    store: DraftStore,
    draft: DraftRecord,
    evaluate: Callable,
    user: dict,
) -> DraftSummary:
    extras = await asyncio.to_thread(_sync_summary_extras, conn, store, draft)
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
        open_blocker_count=extras["open_blocker_count"],
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        ready_at=draft.ready_at,
    )


async def _build_detail(
    conn: sqlite3.Connection,
    store: DraftStore,
    draft: DraftRecord,
    evaluate: Callable,
    user: dict,
) -> DraftDetail:
    extras = await asyncio.to_thread(_sync_detail_extras, conn, store, draft)
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
        open_blocker_count=extras["open_blocker_count"],
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        ready_at=draft.ready_at,
    )
    active_compile = extras["active_compile_job"]
    ledger = extras["ledger"]
    return DraftDetail(
        summary=summary,
        brief=json.loads(draft.brief_json or "{}"),
        inputs=[_to_draft_input(*row) for row in extras["inputs"]],
        current_revision_summary=_to_revision_summary(current) if current else None,
        active_compile_job=_to_job(active_compile) if active_compile else None,
        revision_count=extras["revision_count"],
        evidence_count=ledger["evidence_count"],
        claim_counts_by_status=ledger["claim_counts_by_status"],
        finding_counts_by_severity=ledger["finding_counts_by_severity"],
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


# ── paged listing helper ─────────────────────────────────────────────────────


def _scan_filtered(
    fetch: Callable[[int, int], list[T]],
    keep: Callable[[T], bool],
    *,
    page: int,
    per_page: int,
) -> tuple[list[T], int]:
    """Page a store listing that needs a filter the store does not express.

    Reads exclusively through the store's ``limit``/``offset`` accessors, in
    :data:`_LIST_SCAN_CHUNK`-sized pages, stopping at
    :data:`_MAX_LIST_SCAN_ROWS`. Returns ``(page_items, total_matched)``.
    """
    matched: list[T] = []
    offset = 0
    while offset < _MAX_LIST_SCAN_ROWS:
        chunk = fetch(_LIST_SCAN_CHUNK, offset)
        if not chunk:
            break
        matched.extend(row for row in chunk if keep(row))
        if len(chunk) < _LIST_SCAN_CHUNK:
            break
        offset += _LIST_SCAN_CHUNK
    start = max(page - 1, 0) * per_page
    return matched[start : start + per_page], len(matched)


# ── idempotency and request fingerprints (SPEC section 8.2) ──────────────────


def _validate_idempotency_key(raw: Optional[str]) -> Optional[str]:
    """Validate the ``Idempotency-Key`` header: ASCII, 1-128, printable."""
    if raw is None:
        return None
    if not (1 <= len(raw) <= 128):
        raise DraftRoomHTTPError(
            422,
            "idempotency-key must be 1 to 128 characters",
            "invalid_idempotency_key",
        )
    if not raw.isascii() or any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise DraftRoomHTTPError(
            422,
            "idempotency-key must contain only printable ascii characters",
            "invalid_idempotency_key",
        )
    return raw


def _sync_input_snapshot(store: DraftStore, draft: DraftRecord) -> list[list[Any]]:
    """The draft's inputs reduced to identity + content hashes, ID-ordered."""
    inputs = store.list_inputs(draft_id=draft.id, owner_id=draft.created_by)
    return [
        [r.id, r.role, r.authority, r.content_sha256, r.parsed_text_sha256 or ""]
        for r in sorted(inputs, key=lambda r: r.id)
    ]


def _request_fingerprint(
    *,
    operation: str,
    draft: DraftRecord,
    base_revision_id: Optional[int],
    start_stage: str,
    parent_job_id: Optional[int],
    input_snapshot: list[list[Any]],
) -> str:
    """Canonical fingerprint an ``Idempotency-Key`` is scoped to.

    SPEC section 8.2: "Reusing a key for the same authenticated user and
    identical request fingerprint returns the original job and status; reusing
    it for a different draft, input snapshot, base revision, or requested start
    stage returns 409." Those four dimensions are the payload here, plus the
    operation and prompt-bundle version so a compile and a retry, or two runs
    across a prompt-bundle upgrade, can never alias. The authenticated user is
    not hashed in — it is the other half of the key's scope and is enforced by
    the ``(created_by, idempotency_key)`` unique index.
    """
    return sha256_text(
        canonical_json(
            {
                "operation": operation,
                "draft_id": draft.id,
                "base_revision_id": base_revision_id,
                "start_stage": start_stage,
                "parent_job_id": parent_job_id,
                "brief_sha256": sha256_text(draft.brief_json or ""),
                "mode": draft.mode,
                "tier": draft.tier,
                "prompt_bundle_version": PROMPT_BUNDLE_VERSION,
                "inputs": input_snapshot,
            }
        )
    )


def _sync_compile_fingerprint(store: DraftStore, draft: DraftRecord) -> str:
    """Recompute the orchestrator's canonical compile fingerprint.

    Must stay byte-identical to ``draft_pipeline._build_context``'s
    ``fingerprint``, which is what the pipeline compares
    ``draft_jobs.compile_input_sha256`` against to decide whether stage
    checkpoints may be resumed (SPEC section 10.1 item 6). If the two ever
    drift, the only consequence is that resume is refused and every stage is
    re-run — the safe direction. A temporary equality test against
    ``_build_context`` was run when this was written.
    """
    inputs = store.list_inputs(draft_id=draft.id, owner_id=draft.created_by)
    snapshot: list[list[Any]] = []
    for record in sorted(inputs, key=lambda r: r.id):
        parsed_sha = record.parsed_text_sha256
        if not parsed_sha:
            parsed = (
                store.get_input_parsed_text(
                    draft_id=draft.id, owner_id=draft.created_by, input_id=record.id
                )
                or ""
            )
            parsed_sha = sha256_text(parsed)
        snapshot.append(
            [
                record.id,
                record.role,
                record.authority,
                record.content_sha256,
                parsed_sha,
            ]
        )
    prior = store.get_current_revision(draft_id=draft.id, owner_id=draft.created_by)
    return sha256_text(
        canonical_json(
            {
                "brief_hash": sha256_text(draft.brief_json or ""),
                "mode": draft.mode,
                "tier": draft.tier,
                "prompt_bundle_version": PROMPT_BUNDLE_VERSION,
                "prior_revision_sha256": prior.content_sha256 if prior else "",
                "inputs": snapshot,
            }
        )
    )


def _model_snapshot() -> dict[str, Any]:
    """Non-secret logical-model identifiers recorded with a compile job.

    SPEC section 9.2: "Persist provider kind, model name, prompt ID/hash,
    temperature and timing for audit. Never persist API keys, authorization
    headers, or secret-bearing endpoint query strings." Only model *names* and
    the logical mode are stored here — no URL, host, or credential.
    """
    return {
        "prompt_bundle_version": PROMPT_BUNDLE_VERSION,
        "logical_modes": {
            "thinking": settings.chat_model,
            "instant": settings.instant_chat_model or settings.chat_model,
        },
        "default_logical_mode": settings.draft_default_logical_mode,
    }


def _provider_base_url_for_mode(logical_mode: str) -> str:
    """Provider base URL for a logical mode.

    Mirrors ``draft_pipeline._provider_base_url``: SPEC section 14.2 requires
    single-model deployments to work, so an unconfigured ``instant`` endpoint
    degrades to the ``thinking`` one instead of failing.
    """
    if logical_mode == "instant":
        return (settings.instant_chat_url or settings.ollama_chat_url or "").strip()
    return (settings.ollama_chat_url or "").strip()


def _assert_provider_policy(*, sensitive: bool) -> None:
    """Enforce the section 9.2 provider-origin allowlist BEFORE enqueue.

    The pipeline re-checks before every model call; this gate stops a job that
    could never legally run from being queued at all. The error never echoes
    the configured allowlist or the rejected endpoint (section 9.2: "Never
    return the allowlist or raw endpoint to non-admin clients").
    """
    for logical_mode in ("thinking", "instant"):
        try:
            assert_provider_allowed(
                _provider_base_url_for_mode(logical_mode), sensitive=sensitive
            )
        except ProviderPolicyError as exc:
            raise DraftRoomHTTPError(
                503,
                "the configured model provider is not permitted for this draft",
                exc.code,
            ) from None


# ── transactional compile/disposition/Ready helpers ──────────────────────────
#
# MODULE-OWNERSHIP NOTE (issue #436): ``DraftStore`` — owned by another agent
# and already shipped — has no compile enqueue/retry, no "apply this finding as
# a revision", and no Ready transition. Each of those writes spans several
# tables and MUST be atomic (SPEC sections 8.2 and 12.5: applying a finding
# "creates a new immutable manual revision in the same transaction"; Ready
# "applies this policy transactionally"), so they are implemented here as
# synchronous ``BEGIN IMMEDIATE`` helpers that mirror the store's own
# discipline: one write transaction, explicit prior-status allowlists instead
# of ad-hoc status strings, and rollback on any exception. They take the raw
# connection because a single transaction must span the tables. They should
# move into ``draft_store`` the next time that file is in scope.


def _conflict(detail: str, code: str, **context: Any) -> DraftRoomHTTPError:
    return DraftRoomHTTPError(409, detail, code, context or None)


def _sync_enqueue_compile(
    conn: sqlite3.Connection,
    *,
    draft_id: int,
    owner_id: int,
    lock_version: int,
    base_revision_id: Optional[int],
    requested_start_stage: str,
    normalized_start_stage: str,
    idempotency_key: Optional[str],
    request_fingerprint: str,
    compile_fingerprint: str,
    input_snapshot: list[list[Any]],
    parent_job_id: Optional[int],
    attempt_no: int,
) -> tuple[int, bool]:
    """Create one ``compile`` job atomically. Returns ``(job_id, created)``.

    ``created=False`` means an ``Idempotency-Key`` replay matched an existing
    job with an identical request fingerprint and that job is returned
    unchanged. A key reused with a *different* fingerprint raises 409.
    """
    store = DraftStore(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT vault_id, status, tier, lock_version, ready_revision_id "
            "FROM drafts WHERE id = ? AND created_by = ?",
            (draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("draft not found")
        vault_id, status, _tier, current_lock, ready_revision_id = (
            int(row[0]),
            str(row[1]),
            str(row[2]),
            int(row[3]),
            row[4],
        )

        # Idempotency replay is resolved before any guard so a retried request
        # returns the original job even once the draft has moved on.
        if idempotency_key is not None:
            existing = conn.execute(
                "SELECT id, draft_id, input_json FROM draft_jobs "
                "WHERE created_by = ? AND idempotency_key = ?",
                (owner_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                stored = _json_or(existing[2], {})
                if (
                    int(existing[1]) == draft_id
                    and stored.get("request_fingerprint") == request_fingerprint
                ):
                    conn.rollback()
                    return int(existing[0]), False
                raise _conflict(
                    "this idempotency-key was already used for a different request",
                    "idempotency_key_conflict",
                )

        if current_lock != lock_version:
            raise DraftConflictError(
                "draft was modified by another request; reload and retry"
            )
        if status not in _COMPILE_ALLOWED_PRIOR_STATUSES:
            raise _conflict(
                "draft is not in a state that can start a compile", "invalid_state"
            )

        active = conn.execute(
            "SELECT id FROM draft_jobs WHERE draft_id = ? "
            "AND status IN ('pending','running') LIMIT 1",
            (draft_id,),
        ).fetchone()
        if active is not None:
            raise _conflict(
                "a job is already active for this draft",
                "active_job",
                active_job_id=int(active[0]),
            )

        input_rows = conn.execute(
            "SELECT parse_status FROM draft_inputs WHERE draft_id = ?", (draft_id,)
        ).fetchall()
        if not input_rows:
            raise _conflict("draft has no inputs to compile", "inputs_not_ready")
        if any(str(r[0]) != "ready" for r in input_rows):
            raise _conflict(
                "every input must finish parsing before compile", "inputs_not_ready"
            )

        current = conn.execute(
            "SELECT id FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
            (draft_id,),
        ).fetchone()
        current_id = None if current is None else int(current[0])
        if base_revision_id != current_id:
            raise _conflict(
                "base_revision_id does not match the draft's current revision",
                "stale_base_revision",
            )

        # SPEC section 8.2: "a Ready draft is invalidated first". Ready ->
        # needs_review -> queued keeps every write on a legal edge.
        if status == "ready" or ready_revision_id is not None:
            conn.execute(
                "UPDATE drafts SET status = 'needs_review', ready_revision_id = NULL, "
                "ready_by = NULL, ready_at = NULL WHERE id = ?",
                (draft_id,),
            )
            store._insert_event(  # noqa: SLF001 - see MODULE-OWNERSHIP NOTE
                draft_id=draft_id,
                event_type="ready_invalidated",
                actor_user_id=owner_id,
                revision_id=(
                    int(ready_revision_id) if ready_revision_id is not None else None
                ),
                payload={"reason": "compile_requested"},
            )

        input_json = canonical_json(
            {
                "request_fingerprint": request_fingerprint,
                "requested_start_stage": requested_start_stage,
                "normalized_start_stage": normalized_start_stage,
                "start_stage_normalized": normalized_start_stage
                != requested_start_stage,
                "base_revision_id": base_revision_id,
                "inputs": input_snapshot,
                "prompt_bundle_version": PROMPT_BUNDLE_VERSION,
            }
        )
        brief_row = conn.execute(
            "SELECT brief_json FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        try:
            cur = conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "parent_job_id, attempt_no, idempotency_key, start_stage, "
                "input_revision_id, input_json, brief_snapshot_json, "
                "model_snapshot_json, prompt_bundle_version, compile_input_sha256, "
                "max_model_calls, timeout_seconds) "
                "VALUES (?, ?, ?, 'compile', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft_id,
                    vault_id,
                    owner_id,
                    parent_job_id,
                    attempt_no,
                    idempotency_key,
                    normalized_start_stage,
                    base_revision_id,
                    input_json,
                    str(brief_row[0]) if brief_row else "{}",
                    canonical_json(_model_snapshot()),
                    PROMPT_BUNDLE_VERSION,
                    compile_fingerprint,
                    settings.draft_job_max_model_calls,
                    settings.draft_job_timeout_seconds,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Either the one-active-compile index or the (created_by,
            # idempotency_key) index lost a race. Both are conflicts, never 500s.
            raise _conflict(
                "a compile job is already active for this draft or the "
                "idempotency-key is already in use",
                "active_job",
            ) from exc
        job_id = int(cur.lastrowid)

        conn.execute(
            "UPDATE drafts SET status = 'queued', "
            "lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND created_by = ?",
            (draft_id, owner_id),
        )
        store._insert_event(  # noqa: SLF001 - see MODULE-OWNERSHIP NOTE
            draft_id=draft_id,
            event_type="compile_requested",
            actor_user_id=owner_id,
            job_id=job_id,
            revision_id=base_revision_id,
            payload={
                "requested_start_stage": requested_start_stage,
                "start_stage": normalized_start_stage,
                "start_stage_normalized": normalized_start_stage
                != requested_start_stage,
                "compile_input_sha256": compile_fingerprint,
                "request_fingerprint": request_fingerprint,
                "prompt_bundle_version": PROMPT_BUNDLE_VERSION,
                "parent_job_id": parent_job_id,
                "attempt_no": attempt_no,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return job_id, True


def _sync_normalize_start_stage(
    conn: sqlite3.Connection, *, job_id: Optional[int], requested: str
) -> str:
    """Move ``requested`` back to the first missing/incomplete prerequisite.

    SPEC section 8.1: "The server verifies that every prerequisite checkpoint
    before ``start_stage`` ... is completed. Otherwise it moves ``start_stage``
    backward to the first missing/mismatched prerequisite."

    ``job_id`` is the job whose checkpoints are being reused (the parent job on
    a retry). A fresh compile has none, so it always normalizes back to
    ``research``.
    """
    if job_id is None:
        return _DEFAULT_START_STAGE
    completed = {
        str(row[0])
        for row in conn.execute(
            "SELECT stage FROM draft_job_stages WHERE job_id = ? AND status = "
            "'completed' AND artifact_sha256 IS NOT NULL",
            (job_id,),
        ).fetchall()
    }
    for stage in COMPILE_STAGE_ORDER:
        if stage == requested:
            return requested
        if stage not in completed:
            # The first prerequisite that never completed. ``intake`` is machine
            # -only, so a gap there still surfaces as the earliest user-
            # selectable stage.
            return _DEFAULT_START_STAGE if stage in _MACHINE_ONLY_STAGES else stage
    return requested


def _sync_apply_finding(
    conn: sqlite3.Connection,
    *,
    draft_id: int,
    owner_id: int,
    finding_id: int,
    lock_version: int,
    base_revision_id: Optional[int],
    note: Optional[str],
) -> int:
    """Apply a finding's suggestion as a new immutable manual revision.

    One ``BEGIN IMMEDIATE`` transaction (SPEC section 8.2): verify the span is
    unchanged, splice exactly that span, insert the immutable revision, make it
    current, mark the finding ``applied``, invalidate Ready, and audit. A span
    whose stored hash no longer matches the base revision's bytes is a 409 —
    never a silent overwrite of somebody else's edit.

    Returns the new revision ID.
    """
    store = DraftStore(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        draft_row = conn.execute(
            "SELECT status, lock_version, ready_revision_id FROM drafts "
            "WHERE id = ? AND created_by = ?",
            (draft_id, owner_id),
        ).fetchone()
        if draft_row is None:
            raise DraftNotFoundError("draft not found")
        status, current_lock, ready_revision_id = (
            str(draft_row[0]),
            int(draft_row[1]),
            draft_row[2],
        )
        if current_lock != lock_version:
            raise DraftConflictError(
                "draft was modified by another request; reload and retry"
            )
        if status == "archived":
            raise _conflict(
                "archived draft cannot accept new revisions", "invalid_state"
            )
        active = conn.execute(
            "SELECT id FROM draft_jobs WHERE draft_id = ? AND job_type = 'compile' "
            "AND status IN ('pending','running') LIMIT 1",
            (draft_id,),
        ).fetchone()
        if active is not None:
            raise _conflict("a compile job is active for this draft", "active_job")

        finding = conn.execute(
            "SELECT revision_id, status, suggestion, span_start, span_end, "
            "span_text_sha256, severity, rule_id, rule_version FROM draft_findings "
            "WHERE id = ? AND draft_id = ?",
            (finding_id, draft_id),
        ).fetchone()
        if finding is None:
            raise DraftNotFoundError("finding not found")
        (
            finding_revision_id,
            finding_status,
            suggestion,
            span_start,
            span_end,
            span_text_sha256,
            severity,
            rule_id,
            rule_version,
        ) = finding
        if finding_status != "open":
            raise _conflict(
                "finding has already been dispositioned", "finding_not_open"
            )
        if suggestion is None or span_start is None or span_end is None:
            raise DraftRoomHTTPError(
                422,
                "finding has no suggested replacement span to apply",
                "finding_not_applicable",
            )

        current = conn.execute(
            "SELECT id, content_md FROM draft_revisions "
            "WHERE draft_id = ? AND is_current = 1",
            (draft_id,),
        ).fetchone()
        if current is None:
            raise _conflict(
                "draft has no current revision to apply a finding to",
                "stale_base_revision",
            )
        current_id, content_md = int(current[0]), str(current[1])
        if base_revision_id != current_id:
            raise _conflict(
                "base_revision_id does not match the draft's current revision",
                "stale_base_revision",
            )
        if finding_revision_id is None or int(finding_revision_id) != current_id:
            raise _conflict(
                "finding was raised against a different revision",
                "finding_revision_stale",
            )

        span_start, span_end = int(span_start), int(span_end)
        if not (0 <= span_start < span_end <= len(content_md)):
            raise _conflict(
                "finding span no longer fits the current revision", "stale_span"
            )
        # Optimistic concurrency on the exact bytes the finding was raised
        # against (SPEC section 8.2: "unchanged span hash").
        if span_text_sha256 is not None and sha256_text(
            content_md[span_start:span_end]
        ) != str(span_text_sha256):
            raise _conflict(
                "the text this finding refers to has changed since it was raised",
                "stale_span",
            )

        new_content = content_md[:span_start] + str(suggestion) + content_md[span_end:]
        next_no = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM draft_revisions "
                "WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE draft_revisions SET is_current = 0 WHERE id = ?", (current_id,)
        )
        cur = conn.execute(
            "INSERT INTO draft_revisions (draft_id, parent_revision_id, job_id, "
            "revision_no, source, content_md, content_sha256, fact_status, "
            "is_current, created_by) "
            "VALUES (?, ?, NULL, ?, 'manual', ?, ?, 'not_run', 1, ?)",
            (
                draft_id,
                current_id,
                next_no,
                new_content,
                sha256_text(new_content),
                owner_id,
            ),
        )
        revision_id = int(cur.lastrowid)

        conn.execute(
            "UPDATE draft_findings SET status = 'applied', resolved_by = ?, "
            "resolved_at = CURRENT_TIMESTAMP, resolution_note = ? WHERE id = ?",
            (owner_id, note, finding_id),
        )
        # Any prior factual approval described the old bytes.
        conn.execute(
            "UPDATE drafts SET status = 'needs_review', ready_revision_id = NULL, "
            "ready_by = NULL, ready_at = NULL, lock_version = lock_version + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND created_by = ?",
            (draft_id, owner_id),
        )
        store._insert_event(  # noqa: SLF001 - see MODULE-OWNERSHIP NOTE
            draft_id=draft_id,
            event_type="revision_created",
            actor_user_id=owner_id,
            revision_id=revision_id,
            payload={
                "source": "manual",
                "revision_no": next_no,
                "finding_id": finding_id,
            },
        )
        store._insert_event(  # noqa: SLF001 - see MODULE-OWNERSHIP NOTE
            draft_id=draft_id,
            event_type="finding_applied",
            actor_user_id=owner_id,
            revision_id=revision_id,
            payload={
                "finding_id": finding_id,
                "rule_id": str(rule_id),
                "rule_version": str(rule_version),
                "severity": str(severity),
                "span_text_sha256": span_text_sha256,
            },
        )
        if ready_revision_id is not None:
            store._insert_event(  # noqa: SLF001 - see MODULE-OWNERSHIP NOTE
                draft_id=draft_id,
                event_type="ready_invalidated",
                actor_user_id=owner_id,
                revision_id=revision_id,
                payload={"reason": "finding_applied"},
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return revision_id


def _sync_mark_ready(
    conn: sqlite3.Connection,
    *,
    draft_id: int,
    owner_id: int,
    revision_id: int,
    lock_version: int,
    acknowledge_source_only: bool,
) -> dict[str, Any]:
    """Apply the SPEC section 12.5 Ready policy transactionally.

    This is the ONLY place in the Draft Room backend that writes
    ``drafts.status = 'ready'``, and it is reachable only from the owner's
    authenticated ``POST .../ready`` request — rule 8: "the authenticated owner
    supplies the final human action; the processor, route retry, or finding
    resolver cannot call the Ready transition internally."

    Returns a small audit payload (hashes/IDs only) describing what was checked.
    """
    store = DraftStore(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        draft_row = conn.execute(
            "SELECT status, lock_version, tier FROM drafts "
            "WHERE id = ? AND created_by = ?",
            (draft_id, owner_id),
        ).fetchone()
        if draft_row is None:
            raise DraftNotFoundError("draft not found")
        status, current_lock, tier = (
            str(draft_row[0]),
            int(draft_row[1]),
            str(draft_row[2]),
        )
        if current_lock != lock_version:
            raise DraftConflictError(
                "draft was modified by another request; reload and retry"
            )
        # 'needs_review' is the only prior status with a legal edge to 'ready'.
        if status != "needs_review":
            raise _conflict(
                "only a draft awaiting review can be marked ready", "invalid_state"
            )

        # Rule 1: no active job.
        active = conn.execute(
            "SELECT id FROM draft_jobs WHERE draft_id = ? "
            "AND status IN ('pending','running') LIMIT 1",
            (draft_id,),
        ).fetchone()
        if active is not None:
            raise _conflict("a job is active for this draft", "active_job")

        # Rule 1: the revision is the current one.
        revision = conn.execute(
            "SELECT is_current, content_md, content_sha256, fact_status, job_id, "
            "qa_summary_json FROM draft_revisions WHERE id = ? AND draft_id = ?",
            (revision_id, draft_id),
        ).fetchone()
        if revision is None:
            raise DraftNotFoundError("revision not found")
        (
            is_current,
            content_md,
            content_sha256,
            fact_status,
            job_id,
            qa_summary_json,
        ) = revision
        if not int(is_current):
            raise _conflict(
                "only the draft's current revision can be marked ready",
                "not_current_revision",
            )

        # Rule 2: Fact ran and its successful candidate is byte-identical.
        if str(fact_status) not in _FACT_CURRENT_STATUSES:
            raise _conflict(
                "this revision has no current fact-check result", "fact_not_current"
            )
        if job_id is None:
            raise _conflict(
                "this revision has no compile job to verify fact against",
                "fact_not_current",
            )
        fact_stage = conn.execute(
            "SELECT candidate_sha256 FROM draft_job_stages "
            "WHERE job_id = ? AND stage = 'fact' AND status = 'completed' "
            "ORDER BY attempt DESC LIMIT 1",
            (int(job_id),),
        ).fetchone()
        if fact_stage is None or fact_stage[0] != content_sha256:
            raise _conflict(
                "the successful fact candidate does not match this revision",
                "fact_candidate_mismatch",
            )

        # Rules 3-5: open blockers block. Advisory info/warning findings never do.
        open_blockers = conn.execute(
            "SELECT id, waivable FROM draft_findings WHERE draft_id = ? "
            "AND severity = 'blocker' AND status = 'open'",
            (draft_id,),
        ).fetchall()
        if open_blockers:
            non_waivable = [int(r[0]) for r in open_blockers if not int(r[1])]
            if non_waivable:
                raise _conflict(
                    "a non-waivable blocker must be resolved by a new revision",
                    "non_waivable_blocker",
                    blocker_count=len(non_waivable),
                )
            raise _conflict(
                "every blocking finding must be applied or validly waived",
                "unresolved_blocker",
                blocker_count=len(open_blockers),
            )

        # Rule 6: a waiver only counts with an actor, a reason, the same rule
        # version, and an unchanged span hash.
        for row in conn.execute(
            "SELECT id, resolved_by, resolution_note, rule_version, "
            "waiver_rule_version, waiver_text_sha256, span_start, span_end "
            "FROM draft_findings WHERE draft_id = ? AND revision_id = ? "
            "AND severity = 'blocker' AND status = 'waived'",
            (draft_id, revision_id),
        ).fetchall():
            (
                w_id,
                resolved_by,
                resolution_note,
                rule_version,
                waiver_rule_version,
                waiver_text_sha256,
                w_start,
                w_end,
            ) = row
            if (
                resolved_by is None
                or not str(resolution_note or "").strip()
                or waiver_rule_version != rule_version
            ):
                raise _conflict(
                    "a waived blocker is missing a valid actor, reason, or rule "
                    "version",
                    "invalid_waiver",
                    finding_id=int(w_id),
                )
            if w_start is not None and w_end is not None:
                actual = sha256_text(str(content_md)[int(w_start) : int(w_end)])
                if waiver_text_sha256 != actual:
                    raise _conflict(
                        "a waived blocker's text changed after it was waived",
                        "stale_waiver",
                        finding_id=int(w_id),
                    )

        # Rule 3: unqualified factual claim verdicts are non-waivable blockers.
        unresolved_claims = int(
            conn.execute(
                "SELECT COUNT(*) FROM draft_claims WHERE revision_id = ? "
                "AND resolution = 'open' AND status IN "
                "('contradicted','unsupported','ambiguous','stale')",
                (revision_id,),
            ).fetchone()[0]
        )
        if unresolved_claims:
            raise _conflict(
                "unqualified contradicted, unsupported, ambiguous or stale claims "
                "must be revised before ready",
                "unresolved_claim_blocker",
                claim_count=unresolved_claims,
            )

        # SPEC section 12.6: evidence must still be current inside the Ready
        # transaction. The snapshot records source deletion/update metadata; a
        # source that disappeared after Research invalidates the candidate.
        stale_evidence = int(
            conn.execute(
                "SELECT COUNT(*) FROM draft_evidence WHERE job_id = ? "
                "AND source_deleted_at IS NOT NULL",
                (int(job_id),),
            ).fetchone()[0]
        )
        if stale_evidence:
            conn.execute(
                "UPDATE draft_revisions SET fact_status = 'invalidated' WHERE id = ?",
                (revision_id,),
            )
            store._insert_event(  # noqa: SLF001 - see MODULE-OWNERSHIP NOTE
                draft_id=draft_id,
                event_type="evidence_invalidated",
                actor_user_id=owner_id,
                revision_id=revision_id,
                payload={"reason": "source_deleted", "count": stale_evidence},
            )
            conn.commit()
            raise _conflict(
                "evidence this revision depends on changed or was deleted",
                "evidence_changed",
                evidence_count=stale_evidence,
            )

        # Rule 7: a source-only run needs an explicit acknowledgement.
        qa_summary = _json_or(qa_summary_json, {})
        source_only = bool(qa_summary.get("source_only"))
        if source_only and not acknowledge_source_only:
            raise _conflict(
                "this draft was compiled with no vault evidence and requires "
                "acknowledge_source_only=true",
                "source_only_acknowledgment_required",
            )

        conn.execute(
            "UPDATE drafts SET status = 'ready', ready_revision_id = ?, "
            "ready_by = ?, ready_at = CURRENT_TIMESTAMP, "
            "lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND created_by = ?",
            (revision_id, owner_id, draft_id, owner_id),
        )
        store._insert_event(  # noqa: SLF001 - see MODULE-OWNERSHIP NOTE
            draft_id=draft_id,
            event_type="ready_marked",
            actor_user_id=owner_id,
            revision_id=revision_id,
            payload={
                "content_sha256": str(content_sha256),
                "fact_status": str(fact_status),
                "tier": tier,
                "source_only": source_only,
                "acknowledge_source_only": acknowledge_source_only,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "revision_id": revision_id,
        "content_sha256": str(content_sha256),
        "fact_status": str(fact_status),
        "tier": tier,
        "source_only": source_only,
        "acknowledge_source_only": acknowledge_source_only,
    }


async def _assert_waiver_text_unchanged(
    store: DraftStore, draft_id: int, owner_id: int, finding: DraftFindingRecord
) -> None:
    """A waiver is only valid against unchanged text (SPEC section 8.2/12.5.6).

    The finding's span hash is re-derived from the revision it was raised
    against; if the bytes moved, the waiver would be granted for text nobody
    reviewed, so it is refused with 409 ``stale_span``.
    """
    if (
        finding.revision_id is None
        or finding.span_start is None
        or finding.span_end is None
        or finding.span_text_sha256 is None
    ):
        return
    revision = await _run_store(
        lambda: store.get_revision(
            draft_id=draft_id,
            owner_id=owner_id,
            revision_id=int(finding.revision_id),
            include_content=True,
        )
    )
    content = revision.content_md or ""
    span_start, span_end = int(finding.span_start), int(finding.span_end)
    if not (0 <= span_start < span_end <= len(content)) or sha256_text(
        content[span_start:span_end]
    ) != finding.span_text_sha256:
        raise _conflict(
            "the text this finding refers to has changed since it was raised",
            "stale_span",
        )


def _sync_get_finding(
    conn: sqlite3.Connection, *, draft_id: int, owner_id: int, finding_id: int
) -> DraftFindingRecord:
    """Load one finding, constrained through its owning draft (section 9.1 rule 5)."""
    store = DraftStore(conn)
    store.get_draft(draft_id, owner_id)
    findings, _total = _scan_filtered(
        lambda limit, offset: store.list_findings(
            draft_id=draft_id, owner_id=owner_id, limit=limit, offset=offset
        ),
        lambda record: record.id == finding_id,
        page=1,
        per_page=1,
    )
    if not findings:
        raise DraftNotFoundError("finding not found")
    return findings[0]


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
            "compile_rate_limit": settings.draft_compile_rate_limit,
            "max_sections": settings.draft_max_sections,
            "job_timeout_seconds": settings.draft_job_timeout_seconds,
            "job_max_model_calls": settings.draft_job_max_model_calls,
            "max_correction_loops": settings.draft_qa_retry_limit,
            "max_page_size": 100,
        },
        export_formats=["md"],
        logical_model_modes=["instant", "thinking"],
        default_logical_mode=settings.draft_default_logical_mode,
        compile_start_stages=list(_START_STAGES),
        compile_stage_order=list(COMPILE_STAGE_ORDER),
        # Read-only. The bundle version is owned by draft_prompts, which
        # defines the prompts it names; it is deliberately not an operator
        # setting, because pinning it would let a stale value satisfy the
        # resume gate and reuse checkpoints built from different prompts.
        prompt_bundle_version=PROMPT_BUNDLE_VERSION,
        # "Full editorial gates" means Copy, Standards and Fact are all
        # installed in the orchestrator's canonical order, so no compile can
        # reach Assemble without them.
        editorial_gates_installed=all(
            gate in COMPILE_STAGE_ORDER for gate in ("lint", "copy", "standards", "fact")
        ),
        # Honest capability reporting (SPEC section 8.2). Each flag below is
        # True because the feature is reachable end to end: the route exists
        # here, its store accessors shipped in draft_store, and the compile
        # orchestrator that populates the ledgers shipped in draft_pipeline.
        compile_available=True,
        findings_available=True,
        claims_available=True,
        evidence_available=True,
        ready_available=True,
        # Promotion is issue #437 / SPEC PR 4 and an explicit non-goal here.
        # There is no POST /drafts/{id}/promote route in this module, so
        # advertising it would be a false claim.
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
    return await _build_summary(db, store, draft, evaluate, user)


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
    items = [await _build_summary(db, store, d, evaluate, user) for d in drafts]
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
    return await _build_detail(db, store, draft, evaluate, user)


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
    return await _build_summary(db, store, updated, evaluate, user)


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
    return await _build_summary(db, store, updated, evaluate, user)


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
    return await _build_summary(db, store, updated, evaluate, user)


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
    except DraftInputStorageError as exc:
        # delete_draft tombstones every input before dropping the rows, so a
        # filesystem fault surfaces here as well. Mapped like delete_draft_input
        # does, rather than escaping as an unhandled 500.
        raise _map_storage_error(exc) from exc
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


@router.get(
    "/drafts/{draft_id}/jobs/{job_id}/stages",
    response_model=PaginatedResponse[DraftStage],
)
async def list_draft_job_stages(
    draft_id: int,
    job_id: int,
    include_content: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> PaginatedResponse[DraftStage]:
    """Ordered stage attempts and their validated artifacts (SPEC section 8.2).

    Stage bodies are large, so this is paged and ``content_md`` is returned
    only when ``include_content=true`` — never in a job summary.
    """
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    # Resolve the job through its owning draft first: a child ID alone is
    # never sufficient authorization (SPEC section 9.1 rule 5).
    await _run_store(
        lambda: store.get_job(draft_id=draft_id, owner_id=owner_id, job_id=job_id)
    )
    stages, total = await _run_store(
        lambda: (
            store.list_stages(
                job_id=job_id, limit=per_page, offset=max(page - 1, 0) * per_page
            ),
            _sync_count_stages(db, job_id=job_id),
        )
    )
    return PaginatedResponse[DraftStage](
        items=[_to_stage(s, include_content=include_content) for s in stages],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post("/drafts/{draft_id}/jobs/{job_id}/cancel", response_model=DraftJob)
async def cancel_draft_job(
    request: Request,
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
    await safe_record_security_event(
        db,
        event_type="draft_job_cancelled",
        actor=user,
        request=request,
        metadata={
            "draft_id": draft_id,
            "job_id": job_id,
            "job_type": job.job_type,
            "status": job.status,
        },
    )
    return _to_job(job)


@router.post("/drafts/{draft_id}/jobs/{job_id}/retry", status_code=202, response_model=DraftJob)
@limiter.limit(settings.draft_compile_rate_limit)
async def retry_draft_job(
    request: Request,
    draft_id: int,
    job_id: int,
    body: Optional[RetryJobRequest] = None,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftJob:
    """Retry a failed/cancelled job.

    A parse retry delegates to the store. A compile retry creates a *child*
    job — the terminal job is preserved for audit and never mutated — and
    obeys the same ``Idempotency-Key`` contract as compile. SPEC section 9.2
    makes a compile retry count as a compile request, so the compile rate
    limit applies to this route.
    """
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    parent = await _run_store(
        lambda: store.get_job(draft_id=draft_id, owner_id=owner_id, job_id=job_id)
    )

    if parent.job_type != "compile":
        job = await _run_store(
            lambda: store.retry_parse_job(
                draft_id=draft_id,
                owner_id=owner_id,
                job_id=job_id,
                timeout_seconds=settings.draft_parse_timeout_seconds,
            )
        )
        return _to_job(job)

    if parent.status not in ("failed", "cancelled"):
        raise _conflict(
            "only a failed or cancelled job can be retried", "invalid_state"
        )
    _assert_provider_policy(sensitive=draft.tier == "sensitive")

    requested = (body.start_stage if body else None) or _RETRY_STAGE_NORMALIZATION.get(
        parent.active_stage or "", parent.active_stage or _DEFAULT_START_STAGE
    )
    requested = _RETRY_STAGE_NORMALIZATION.get(requested, requested)
    if requested not in _START_STAGES:
        requested = _DEFAULT_START_STAGE

    idempotency_key = _validate_idempotency_key(request.headers.get(_IDEMPOTENCY_HEADER))
    prepared = await _run_store(
        lambda: {
            "input_snapshot": _sync_input_snapshot(store, draft),
            "compile_fingerprint": _sync_compile_fingerprint(store, draft),
            "current_revision": store.get_current_revision(
                draft_id=draft_id, owner_id=owner_id
            ),
            "normalized": _sync_normalize_start_stage(
                db, job_id=job_id, requested=requested
            ),
        }
    )
    current_revision = prepared["current_revision"]
    base_revision_id = current_revision.id if current_revision else None
    fingerprint = _request_fingerprint(
        operation="retry",
        draft=draft,
        base_revision_id=base_revision_id,
        start_stage=prepared["normalized"],
        parent_job_id=job_id,
        input_snapshot=prepared["input_snapshot"],
    )
    new_job_id, created = await _run_store(
        lambda: _sync_enqueue_compile(
            db,
            draft_id=draft_id,
            owner_id=owner_id,
            lock_version=draft.lock_version,
            base_revision_id=base_revision_id,
            requested_start_stage=requested,
            normalized_start_stage=prepared["normalized"],
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            compile_fingerprint=prepared["compile_fingerprint"],
            input_snapshot=prepared["input_snapshot"],
            parent_job_id=job_id,
            attempt_no=parent.attempt_no + 1,
        )
    )
    job = await _run_store(
        lambda: store.get_job(draft_id=draft_id, owner_id=owner_id, job_id=new_job_id)
    )
    if created:
        await safe_record_security_event(
            db,
            event_type="draft_job_retried",
            actor=user,
            request=request,
            metadata={
                "draft_id": draft_id,
                "job_id": new_job_id,
                "parent_job_id": job_id,
                "start_stage": prepared["normalized"],
                "requested_start_stage": requested,
                "compile_input_sha256": prepared["compile_fingerprint"],
                "prompt_bundle_version": PROMPT_BUNDLE_VERSION,
            },
        )
    return _to_job(job)


# ── compile ──────────────────────────────────────────────────────────────────


@router.post("/drafts/{draft_id}/compile", status_code=202, response_model=DraftJob)
@limiter.limit(settings.draft_compile_rate_limit)
async def compile_draft(
    request: Request,
    draft_id: int,
    body: CompileRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftJob:
    """Enqueue one compile job (SPEC section 8.2).

    Snapshots the brief, the input/revision hashes, the prompt bundle and the
    non-secret model IDs onto the job row. The provider-origin allowlist is
    enforced *before* enqueue so a job that could never legally call a model is
    never queued; the orchestrator re-checks before every call.

    ``start_stage`` can never be ``assemble`` — the accepted enum excludes it —
    and is normalized backward to the first prerequisite checkpoint that is
    missing, which for a fresh compile is always ``research``.
    """
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    _assert_provider_policy(sensitive=draft.tier == "sensitive")

    idempotency_key = _validate_idempotency_key(request.headers.get(_IDEMPOTENCY_HEADER))
    prepared = await _run_store(
        lambda: {
            "input_snapshot": _sync_input_snapshot(store, draft),
            "compile_fingerprint": _sync_compile_fingerprint(store, draft),
            "normalized": _sync_normalize_start_stage(
                db, job_id=None, requested=body.start_stage
            ),
        }
    )
    fingerprint = _request_fingerprint(
        operation="compile",
        draft=draft,
        base_revision_id=body.base_revision_id,
        start_stage=prepared["normalized"],
        parent_job_id=None,
        input_snapshot=prepared["input_snapshot"],
    )
    job_id, created = await _run_store(
        lambda: _sync_enqueue_compile(
            db,
            draft_id=draft_id,
            owner_id=owner_id,
            lock_version=body.lock_version,
            base_revision_id=body.base_revision_id,
            requested_start_stage=body.start_stage,
            normalized_start_stage=prepared["normalized"],
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            compile_fingerprint=prepared["compile_fingerprint"],
            input_snapshot=prepared["input_snapshot"],
            parent_job_id=None,
            attempt_no=1,
        )
    )
    job = await _run_store(
        lambda: store.get_job(draft_id=draft_id, owner_id=owner_id, job_id=job_id)
    )
    if created:
        await safe_record_security_event(
            db,
            event_type="draft_compile_requested",
            actor=user,
            request=request,
            metadata={
                "draft_id": draft_id,
                "job_id": job_id,
                "start_stage": prepared["normalized"],
                "requested_start_stage": body.start_stage,
                "base_revision_id": body.base_revision_id,
                "compile_input_sha256": prepared["compile_fingerprint"],
                "prompt_bundle_version": PROMPT_BUNDLE_VERSION,
                "tier": draft.tier,
                "idempotent_replay": False,
            },
        )
    else:
        # Idempotent replay. The job's request snapshot is a large body, so it
        # is fetched explicitly through the store's dedicated accessor rather
        # than being carried on the DraftJob summary (SPEC section 8.2).
        stored = await _run_store(
            lambda: store.get_job_json_field(job_id=job_id, field="input_json")
        )
        await safe_record_security_event(
            db,
            event_type="draft_compile_requested",
            actor=user,
            request=request,
            metadata={
                "draft_id": draft_id,
                "job_id": job_id,
                "start_stage": stored.get("normalized_start_stage"),
                "idempotent_replay": True,
            },
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


@router.post("/drafts/{draft_id}/revisions/{revision_id}/ready", response_model=DraftSummary)
async def mark_revision_ready(
    request: Request,
    draft_id: int,
    revision_id: int,
    body: ReadyRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> DraftSummary:
    """Mark a revision human-approved (SPEC section 12.5).

    The complete precondition list, applied in one transaction by
    :func:`_sync_mark_ready`:

    1. the caller owns the draft and still holds vault ``read``;
    2. the revision is the draft's *current* revision;
    3. the draft has no pending/running job;
    4. ``fact_status`` is ``passed`` or ``findings``;
    5. the successful Fact stage's ``candidate_sha256`` equals the revision's
       ``content_sha256`` byte for byte;
    6. no open ``blocker`` finding remains — a non-waivable one can only be
       cleared by a new revision, a waivable one by a valid waiver;
    7. every waived blocker on this revision has an actor, a non-empty reason,
       the same rule version, and an unchanged span-text hash;
    8. no claim on the revision is still ``open`` with a ``contradicted``,
       ``unsupported``, ``ambiguous`` or ``stale`` verdict;
    9. no evidence the revision depends on has been deleted since Research
       (which instead invalidates Fact and refuses Ready);
    10. a source-only compile requires ``acknowledge_source_only=true``.

    This route is the only path to ``status='ready'`` anywhere in Draft Room —
    rule 8: no processor, retry, or finding resolver can reach it.
    """
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    audit = await _run_store(
        lambda: _sync_mark_ready(
            db,
            draft_id=draft_id,
            owner_id=owner_id,
            revision_id=revision_id,
            lock_version=body.lock_version,
            acknowledge_source_only=body.acknowledge_source_only,
        )
    )
    await safe_record_security_event(
        db,
        event_type="draft_ready_marked",
        actor=user,
        request=request,
        metadata={"draft_id": draft_id, **audit},
    )
    updated = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    return await _build_summary(db, store, updated, evaluate, user)


@router.post("/drafts/{draft_id}/revisions/{revision_id}/export")
async def export_draft_revision(
    request: Request,
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

    Filename matrix (SPEC section 8.2):

    ============================  ===================  ==========================
    Revision state                Acknowledgement      Filename
    ============================  ===================  ==========================
    fact ``not_run``/``running``  ``acknowledge_not_   ``<title>-rev<N>-UNVERIFIED.md``
    /``invalidated``              fact_checked=true``
    fact ``passed``/``findings``  not required         ``<title>-rev<N>-REVIEW.md``
    but not the Ready revision
    the draft's current           not required         ``<title>-rev<N>.md``
    human-Ready revision
    ============================  ===================  ==========================

    ``X-Draft-Fact-Status`` carries the stored ``fact_status`` verbatim and
    ``X-Draft-Approval-Status`` is ``ready`` or ``not_ready``. The Markdown
    body is returned byte for byte and is never prefixed with a warning or
    otherwise mutated.
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
    fact_current = fact_status in _FACT_CURRENT_STATUSES
    # "Only the draft's current human-Ready revision uses the ordinary
    # title/revision filename" — Ready is a property of the draft, so it needs
    # all three: the human approval pointer, the Ready status, and currency.
    is_ready = bool(
        draft.ready_revision_id == revision.id
        and draft.status == "ready"
        and revision.is_current
    )
    if not fact_current:
        if not acknowledge_not_fact_checked:
            raise DraftRoomHTTPError(
                422,
                "export requires acknowledge_not_fact_checked=true because "
                "this revision has no current fact-check result",
                "export_ack_required",
            )
        tag = "UNVERIFIED"
    elif is_ready:
        tag = ""
    else:
        tag = "REVIEW"
    approval_status = "ready" if is_ready else "not_ready"

    filename = _export_filename(draft.title, revision.revision_no, tag)
    await _run_store(
        lambda: store.record_event(
            draft_id=draft_id,
            owner_id=owner_id,
            event_type="exported",
            actor_user_id=owner_id,
            revision_id=revision.id,
            payload={
                "format": "md",
                "fact_status": fact_status,
                "approval_status": approval_status,
                "content_sha256": revision.content_sha256,
                "acknowledged_not_fact_checked": bool(acknowledge_not_fact_checked),
            },
        )
    )
    await safe_record_security_event(
        db,
        event_type="draft_exported",
        actor=user,
        request=request,
        metadata={
            "draft_id": draft_id,
            "revision_id": revision.id,
            "format": "md",
            "fact_status": fact_status,
            "approval_status": approval_status,
            "content_sha256": revision.content_sha256,
        },
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


# ── evidence, claims, findings ───────────────────────────────────────────────


@router.get(
    "/drafts/{draft_id}/evidence", response_model=PaginatedResponse[DraftEvidence]
)
async def list_draft_evidence(
    draft_id: int,
    job_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> PaginatedResponse[DraftEvidence]:
    """Paginated evidence snapshots with a deleted-source marker.

    Defaults to the job that produced the draft's current revision. Evidence
    passages are large, so they are only ever reachable through this paged
    route — never embedded in a draft summary or detail.
    """
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)

    if job_id is not None:
        # Resolve through the owning draft before the evidence query.
        await _run_store(
            lambda: store.get_job(draft_id=draft_id, owner_id=owner_id, job_id=job_id)
        )
        resolved_job_id: Optional[int] = job_id
    else:
        current = await _run_store(
            lambda: store.get_current_revision(draft_id=draft_id, owner_id=owner_id)
        )
        resolved_job_id = current.job_id if current else None
    if resolved_job_id is None:
        return PaginatedResponse[DraftEvidence](
            items=[], total=0, page=page, per_page=per_page
        )

    records, total = await _run_store(
        lambda: (
            store.list_evidence(
                job_id=resolved_job_id,
                limit=per_page,
                offset=max(page - 1, 0) * per_page,
            ),
            _sync_count_evidence(db, job_id=resolved_job_id),
        )
    )
    return PaginatedResponse[DraftEvidence](
        items=[_to_evidence(r) for r in records],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/drafts/{draft_id}/claims", response_model=PaginatedResponse[DraftClaim])
async def list_draft_claims(
    draft_id: int,
    revision_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> PaginatedResponse[DraftClaim]:
    """Paginated atomic claims with their authorized source links.

    Each source carries ``lexical_overlap_score`` — a per-citation lexical
    diagnostic, never a confidence or verification score (SPEC section 12.3).
    """
    if status is not None and status not in CLAIM_STATUSES:
        raise DraftRoomHTTPError(
            422, f"status must be one of {sorted(CLAIM_STATUSES)}", "validation_failed"
        )
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)

    if revision_id is not None:
        # Constrain the revision through the owning draft before reading claims.
        revision = await _run_store(
            lambda: store.get_revision(
                draft_id=draft_id,
                owner_id=owner_id,
                revision_id=revision_id,
                include_content=False,
            )
        )
    else:
        revision = await _run_store(
            lambda: store.get_current_revision(draft_id=draft_id, owner_id=owner_id)
        )
    if revision is None:
        return PaginatedResponse[DraftClaim](
            items=[], total=0, page=page, per_page=per_page
        )
    target_revision_id = revision.id

    def _load() -> tuple[list[DraftClaimRecord], int]:
        return _scan_filtered(
            lambda limit, offset: store.list_claims(
                revision_id=target_revision_id, limit=limit, offset=offset
            ),
            lambda record: status is None or record.status == status,
            page=page,
            per_page=per_page,
        )

    claims, total = await _run_store(_load)
    sources = await _run_store(
        lambda: {
            claim.id: store.list_claim_sources(
                claim_id=claim.id, limit=_MAX_CLAIM_SOURCES
            )
            for claim in claims
        }
    )
    return PaginatedResponse[DraftClaim](
        items=[_to_claim(c, sources.get(c.id, [])) for c in claims],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/drafts/{draft_id}/findings", response_model=PaginatedResponse[DraftFinding]
)
async def list_draft_findings(
    draft_id: int,
    revision_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
) -> PaginatedResponse[DraftFinding]:
    """Paginated findings plus their disposition eligibility."""
    if status is not None and status not in FINDING_STATUSES:
        raise DraftRoomHTTPError(
            422,
            f"status must be one of {sorted(FINDING_STATUSES)}",
            "validation_failed",
        )
    if severity is not None and severity not in FINDING_SEVERITIES:
        raise DraftRoomHTTPError(
            422,
            f"severity must be one of {sorted(FINDING_SEVERITIES)}",
            "validation_failed",
        )
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)

    def _keep(record: DraftFindingRecord) -> bool:
        if status is not None and record.status != status:
            return False
        if severity is not None and record.severity != severity:
            return False
        return True

    def _load() -> tuple[list[DraftFindingRecord], int]:
        return _scan_filtered(
            lambda limit, offset: store.list_findings(
                draft_id=draft_id,
                owner_id=owner_id,
                revision_id=revision_id,
                limit=limit,
                offset=offset,
            ),
            _keep,
            page=page,
            per_page=per_page,
        )

    findings, total = await _run_store(_load)
    return PaginatedResponse[DraftFinding](
        items=[_to_finding(f) for f in findings],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post(
    "/drafts/{draft_id}/findings/{finding_id}/disposition",
    response_model=FindingDispositionResponse,
)
async def dispose_draft_finding(
    request: Request,
    response: Response,
    draft_id: int,
    finding_id: int,
    body: FindingDispositionRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
) -> FindingDispositionResponse:
    """Apply, dismiss, or waive a finding (SPEC section 8.2).

    * ``apply`` needs a current-revision finding with a suggestion and an
      unchanged span hash. It splices exactly that span into a new immutable
      manual revision in one transaction and invalidates Ready/Fact. A stale
      span is 409 ``stale_span``, never a silent overwrite. Responds 201.
    * ``dismiss`` is prohibited for blockers (409 ``blocker_not_dismissable``).
    * ``waive`` needs ``waivable=1``, an unchanged span text and rule version,
      a user actor, and a non-empty ``note``.

    Pipeline-authored resolutions use the internal ``resolved_by_revision``
    status and never travel through this route, so they cannot impersonate a
    user action.
    """
    _require_enabled()
    owner_id = int(user["id"])
    store = DraftStore(db)
    draft = await _run_store(lambda: store.get_draft(draft_id, owner_id))
    await _require_vault_read(evaluate, user, draft.vault_id)
    finding = await _run_store(
        lambda: _sync_get_finding(
            db, draft_id=draft_id, owner_id=owner_id, finding_id=finding_id
        )
    )
    if draft.lock_version != body.lock_version:
        raise _conflict(
            "draft was modified by another request; reload and retry",
            "stale_lock_version",
        )

    new_revision: Optional[DraftRevisionRecord] = None
    if body.action == "apply":
        revision_id = await _run_store(
            lambda: _sync_apply_finding(
                db,
                draft_id=draft_id,
                owner_id=owner_id,
                finding_id=finding_id,
                lock_version=body.lock_version,
                base_revision_id=body.base_revision_id,
                note=body.note,
            )
        )
        new_revision = await _run_store(
            lambda: store.get_revision(
                draft_id=draft_id,
                owner_id=owner_id,
                revision_id=revision_id,
                include_content=False,
            )
        )
        response.status_code = 201
    elif body.action == "dismiss":
        if finding.severity == "blocker":
            raise _conflict(
                "a blocker cannot be dismissed; resolve it with a revision or a "
                "valid waiver",
                "blocker_not_dismissable",
            )
        await _run_store(
            lambda: store.dismiss_finding(
                finding_id=finding_id,
                resolved_by=owner_id,
                resolution_note=body.note,
            )
        )
    else:
        if not (body.note or "").strip():
            raise DraftRoomHTTPError(
                422, "waiving a finding requires a non-empty note", "waiver_reason_required"
            )
        if not finding.waivable:
            raise _conflict("this finding is not waivable", "finding_not_waivable")
        await _assert_waiver_text_unchanged(store, draft_id, owner_id, finding)
        await _run_store(
            lambda: store.waive_finding(
                finding_id=finding_id,
                resolved_by=owner_id,
                resolution_note=str(body.note),
                # SPEC section 8.2: a waiver pins the rule version and the exact
                # text it was granted against, so a later edit invalidates it.
                waiver_rule_version=finding.rule_version,
                waiver_text_sha256=finding.span_text_sha256,
            )
        )

    await _run_store(
        lambda: store.record_event(
            draft_id=draft_id,
            owner_id=owner_id,
            event_type=_DISPOSITION_EVENT_TYPES[body.action],
            actor_user_id=owner_id,
            revision_id=new_revision.id if new_revision else finding.revision_id,
            payload={
                "finding_id": finding_id,
                "rule_id": finding.rule_id,
                "rule_version": finding.rule_version,
                "severity": finding.severity,
                "reason": (body.note or "")[:500],
            },
        )
    )
    await safe_record_security_event(
        db,
        event_type=f"draft_finding_{body.action}",
        actor=user,
        request=request,
        metadata={
            "draft_id": draft_id,
            "finding_id": finding_id,
            "action": body.action,
            "rule_id": finding.rule_id,
            "rule_version": finding.rule_version,
            "severity": finding.severity,
            "span_text_sha256": finding.span_text_sha256,
            "revision_id": new_revision.id if new_revision else finding.revision_id,
        },
    )
    updated = await _run_store(
        lambda: _sync_get_finding(
            db, draft_id=draft_id, owner_id=owner_id, finding_id=finding_id
        )
    )
    return FindingDispositionResponse(
        finding=_to_finding(updated),
        revision=_to_revision_summary(new_revision) if new_revision else None,
    )
