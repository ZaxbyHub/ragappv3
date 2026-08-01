"""SQLite persistence for Draft Room (issue #435, ``specs/draft-room/SPEC.md``).

``DraftStore`` owns *all* Draft Room CRUD, atomic job claims, centralized status
transitions, revision numbering, and audit-event records. Routes and the durable
job processor go through it; no other module writes these tables.

Invariants enforced here that SQLite cannot express (SPEC sections 5.7, 9.1, 10.3):

* Every read/write is scoped by both ``draft_id`` **and** ``created_by``. A child
  row (input, job, revision, event) is only ever reachable through its owning
  draft, so a child ID is never sufficient authorization on its own.
* ``draft_jobs.vault_id``/``created_by`` equal the owning draft's values, and
  ``input_id`` is non-null only for ``parse_input`` jobs belonging to that draft.
* ``draft_revisions.job_id`` is null for manual revisions and otherwise names a
  job on the same draft; ``parent_revision_id`` belongs to the same draft.
* A draft's owner and vault are immutable after creation.
* No caller may write an arbitrary status string — every status change goes
  through the transition tables below and is rejected otherwise.
* Revision-number allocation and current-revision replacement happen inside a
  single ``BEGIN IMMEDIATE`` transaction.

The store is synchronous (SQLite is sync). Async callers wrap it in
``asyncio.to_thread`` per repository convention, and the durable processor never
holds a connection across an ``await``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ── Errors ───────────────────────────────────────────────────────────────────


class DraftStoreError(Exception):
    """Base class for Draft Room store failures."""

    code = "internal_error"


class DraftNotFoundError(DraftStoreError):
    """The draft (or a child row reached through it) does not exist for this owner.

    Callers translate this to ``404``: a draft owned by another user and a draft
    that never existed are deliberately indistinguishable.
    """

    code = "not_found"


class DraftConflictError(DraftStoreError):
    """A concurrency or state conflict — stale ``lock_version``, active job,
    duplicate input, or an invalid state transition. Callers translate to ``409``."""

    code = "conflict"


class DraftLimitExceededError(DraftStoreError):
    """A configured project limit was exceeded. Callers translate to ``413``."""

    code = "limit_exceeded"


class DraftValidationError(DraftStoreError):
    """A value violated a store-layer invariant. Callers translate to ``422``."""

    code = "validation_failed"


class InvalidTransitionError(DraftConflictError):
    """A status change was requested that the state machine forbids."""

    code = "invalid_transition"


# ── Status vocabularies and transitions (SPEC section 10.3) ──────────────────

DRAFT_STATUSES: frozenset[str] = frozenset(
    {
        "draft",
        "queued",
        "running",
        "needs_review",
        "ready",
        "failed",
        "cancelled",
        "archived",
    }
)
JOB_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "completed", "failed", "cancelled"}
)
INPUT_STATUSES: frozenset[str] = frozenset(
    {"pending", "parsing", "ready", "failed", "cancelled"}
)

DRAFT_MODES: frozenset[str] = frozenset({"rewrite", "compose"})
DRAFT_TIERS: frozenset[str] = frozenset({"standard", "high_stakes", "sensitive"})
INPUT_ROLES: frozenset[str] = frozenset(
    {"manuscript", "reference", "style", "background", "challenge"}
)
INPUT_AUTHORITIES: frozenset[str] = frozenset(
    {"primary", "official", "secondary", "user_asserted", "unknown"}
)
JOB_TYPES: frozenset[str] = frozenset({"parse_input", "compile"})
REVISION_SOURCES: frozenset[str] = frozenset({"pipeline", "manual"})

# Transitions reachable through ordinary operation.
_DRAFT_TRANSITIONS: dict[str, frozenset[str]] = {
    # 'draft' -> 'needs_review' is the manual-authoring edge. SPEC section 10.3
    # lists the compile lifecycle (draft -> queued -> running -> needs_review),
    # but section 3.3 states that saving a manual revision sets the project to
    # needs_review, and this release ships manual revisions with no compile
    # path at all. Without this edge the very first manual save on a new draft
    # is rejected and the feature is unreachable.
    "draft": frozenset({"queued", "needs_review", "archived"}),
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"needs_review", "failed", "cancelled"}),
    "needs_review": frozenset({"queued", "ready", "archived"}),
    "ready": frozenset({"needs_review", "archived"}),
    "failed": frozenset({"queued", "archived"}),
    "cancelled": frozenset({"queued", "archived"}),
    "archived": frozenset({"draft", "needs_review"}),
}
_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_INPUT_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"parsing", "failed", "cancelled"}),
    "parsing": frozenset({"ready", "failed", "cancelled"}),
    "ready": frozenset(),
    "failed": frozenset({"pending"}),
    "cancelled": frozenset({"pending"}),
}

# Transitions permitted *only* to startup orphan recovery (SPEC section 10.3).
# They are separated so ordinary code paths cannot reach them by accident.
_DRAFT_RECOVERY_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset({"queued"}),
}
_JOB_RECOVERY_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset({"pending"}),
}
_INPUT_RECOVERY_TRANSITIONS: dict[str, frozenset[str]] = {
    "parsing": frozenset({"pending"}),
}

# A parse commits the input's status and the job's status in two separate
# transactions, so a crash between them leaves the input terminal while the job
# is still 'running'. Re-queueing such a job would be wrong twice over: the work
# already finished, and every terminal parse state has no outgoing transition
# (see ``_INPUT_TRANSITIONS``), so the retry could not legally move the input
# back out and the job would fail permanently. Recovery instead settles the job
# onto the outcome the input already records.
_TERMINAL_PARSE_TO_JOB_STATUS: dict[str, str] = {
    "ready": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}

# Statuses that mean a job is still consuming resources.
ACTIVE_JOB_STATUSES: tuple[str, ...] = ("pending", "running")

# ── Pipeline / factuality vocabularies (SPEC sections 5.5-5.8, issue #436) ────

STAGE_NAMES: frozenset[str] = frozenset(
    {
        "intake",
        "research",
        "outline",
        "draft",
        "lint",
        "copy",
        "standards",
        "fact",
        "assemble",
    }
)
STAGE_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "completed", "failed", "skipped", "cancelled"}
)
EVIDENCE_SOURCE_KINDS: frozenset[str] = frozenset(
    {"draft_input", "document", "wiki", "kms"}
)
CLAIM_TYPES: frozenset[str] = frozenset({"factual", "quote", "opinion"})
CLAIM_STATUSES: frozenset[str] = frozenset(
    {"supported", "contradicted", "ambiguous", "stale", "unsupported", "opinion"}
)
CLAIM_SEVERITIES: frozenset[str] = frozenset({"info", "warning", "blocker"})
CLAIM_RESOLUTIONS: frozenset[str] = frozenset(
    {"open", "resolved_by_revision", "accepted", "waived"}
)
CLAIM_SOURCE_RELATIONSHIPS: frozenset[str] = frozenset(
    {"supports", "contradicts", "context"}
)
FINDING_CATEGORIES: frozenset[str] = frozenset(
    {
        "boilerplate",
        "style",
        "preservation",
        "factuality",
        "quote",
        "conflict",
        "security",
        "operational",
    }
)
FINDING_SEVERITIES: frozenset[str] = frozenset({"info", "warning", "blocker"})
FINDING_STATUSES: frozenset[str] = frozenset(
    {"open", "applied", "dismissed", "waived", "resolved_by_revision"}
)

# ── Evidence freshness / invalidation vocabulary (SPEC section 12.6) ─────────
#
# The two reasons a snapshotted evidence identity can stop being current. Both
# open a NON-WAIVABLE blocker: SPEC 12.6 requires the text to be revised, not
# waived, once the evidence under it moved.
EVIDENCE_INVALIDATION_REASONS: frozenset[str] = frozenset(
    {"evidence_changed", "source_deleted"}
)
EVIDENCE_INVALIDATION_RULE_VERSION: str = "1"
_EVIDENCE_INVALIDATION_MESSAGES: dict[str, str] = {
    "evidence_changed": (
        "A source this revision cites changed after it was researched. The "
        "revision must be re-compiled against fresh evidence before it can be "
        "approved."
    ),
    "source_deleted": (
        "A source this revision cites no longer exists in the vault. The "
        "revision must be re-compiled against fresh evidence before it can be "
        "approved."
    ),
}

# Identity predicates for ``list_evidence_identities_for_source``. Each is
# written against exactly one of the partial identity indexes shipped in
# ``_DRAFT_ROOM_PIPELINE_DDL``. Wiki appears twice on purpose: a page change
# invalidates every row with that ``wiki_page_id`` (including page-level rows
# where ``wiki_claim_id IS NULL``), while a claim change invalidates only rows
# carrying that ``wiki_claim_id``.
EVIDENCE_SOURCE_FILTERS: dict[str, str] = {
    "draft_input": "e.source_kind = 'draft_input' AND e.draft_input_id = ?",
    "document": "e.source_kind = 'document' AND e.file_id = ?",
    "wiki_page": "e.source_kind = 'wiki' AND e.wiki_page_id = ?",
    "wiki_claim": "e.wiki_claim_id = ?",
    "kms": "e.source_kind = 'kms' AND e.kms_entry_id = ?",
}

_STAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "failed", "skipped", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "skipped": frozenset(),
    "cancelled": frozenset(),
}
# Reserved for startup recovery: an attempt left 'running' by a crashed worker
# is marked 'failed' with error_code='worker_restart' rather than resurrected
# — a completed stage row is immutable, so a retry must use a new attempt
# number, never overwrite the abandoned one.
_STAGE_RECOVERY_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset({"failed"}),
}
_CLAIM_RESOLUTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"resolved_by_revision", "accepted", "waived"}),
    "resolved_by_revision": frozenset(),
    "accepted": frozenset(),
    "waived": frozenset(),
}
_CLAIM_RESOLUTION_RECOVERY_TRANSITIONS: dict[str, frozenset[str]] = {}
_FINDING_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"applied", "dismissed", "waived", "resolved_by_revision"}),
    "applied": frozenset(),
    "dismissed": frozenset(),
    "waived": frozenset(),
    "resolved_by_revision": frozenset(),
}
_FINDING_RECOVERY_TRANSITIONS: dict[str, frozenset[str]] = {}

_MAX_ERROR_MESSAGE_LEN = 2000
_MAX_TITLE_LEN = 300


def _check_transition(
    kind: str,
    current: str,
    target: str,
    table: dict[str, frozenset[str]],
    recovery_table: dict[str, frozenset[str]],
    *,
    allow_recovery: bool = False,
) -> None:
    """Reject any status change the state machine does not allow.

    Args:
        kind: Entity name used in the error message (``draft``/``job``/``input``).
        current: The row's present status.
        target: The requested status.
        table: Ordinary transition map.
        recovery_table: Transitions reserved for startup orphan recovery.
        allow_recovery: True only when called from startup recovery.

    Raises:
        InvalidTransitionError: If the transition is not permitted.
    """
    if target == current:
        return
    allowed = set(table.get(current, frozenset()))
    if allow_recovery:
        allowed |= set(recovery_table.get(current, frozenset()))
    if target not in allowed:
        raise InvalidTransitionError(
            f"{kind} cannot move from '{current}' to '{target}'"
        )


def sha256_text(text: str) -> str:
    """SHA-256 of ``text`` encoded as UTF-8, as lowercase hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _truncate(value: Optional[str], limit: int) -> Optional[str]:
    """Bound a stored diagnostic string without raising on ``None``."""
    if value is None:
        return None
    return value[:limit]


def validate_evidence_identity(
    *,
    source_kind: str,
    draft_input_id: Optional[int],
    file_id: Optional[int],
    wiki_page_id: Optional[int],
    wiki_claim_id: Optional[int],
    kms_entry_id: Optional[int],
) -> None:
    """Enforce exactly one populated evidence identity family (SPEC 5.6).

    Draft input requires ``draft_input_id``; document requires ``file_id``;
    Wiki requires ``wiki_page_id`` and permits ``wiki_claim_id``; KMS requires
    ``kms_entry_id``; every other identity field must be null.

    Raises:
        DraftValidationError: ``source_kind`` is unknown, zero or more than one
            identity family is populated, or ``wiki_claim_id`` is set for a
            non-wiki row.
    """
    families = {
        "draft_input": draft_input_id is not None,
        "document": file_id is not None,
        "wiki": wiki_page_id is not None,
        "kms": kms_entry_id is not None,
    }
    if source_kind not in families:
        raise DraftValidationError(f"unknown evidence source_kind: {source_kind!r}")
    populated = [name for name, present in families.items() if present]
    if populated != [source_kind]:
        raise DraftValidationError(
            "evidence must populate exactly one identity family matching "
            f"source_kind {source_kind!r} (found: {populated or 'none'})"
        )
    if source_kind != "wiki" and wiki_claim_id is not None:
        raise DraftValidationError(
            "wiki_claim_id is only permitted when source_kind is 'wiki'"
        )


def validate_claim_span(content_md: str, span_start: int, span_end: int) -> str:
    """Validate a claim/finding span against exact revision bytes and hash it.

    Every claim or finding span must map to an exact, verifiable slice of the
    revision's stored Markdown rather than free-form model output (SPEC 5.7).

    Returns:
        ``sha256_text`` of ``content_md[span_start:span_end]``.

    Raises:
        DraftValidationError: The span is out of bounds or empty.
    """
    if span_start < 0 or span_end <= span_start or span_end > len(content_md):
        raise DraftValidationError(
            "span is out of bounds for the referenced revision content"
        )
    return sha256_text(content_md[span_start:span_end])


#: Unicode quote marks folded to their ASCII equivalent for comparison only
#: (SPEC 12.4: "Normalize line endings and Unicode quote marks only for
#: comparison"). Typographic substitution is a rendering difference, not an
#: alteration of what was said, so a curly apostrophe in the manuscript must
#: still match a straight one in the source.
_QUOTE_FOLD = str.maketrans(
    {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
        "\u2032": "'", "\u00b4": "'", "\u02bc": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
        "\u2033": '"', "\u00ab": '"', "\u00bb": '"',
    }
)

#: Ellipsis forms that may mark an omission inside a direct quote.
_ELLIPSIS_FORMS = ("\u2026", "...", ". . .")


def _normalize_quote(text: str) -> str:
    """Fold Unicode quote marks and line endings, then collapse whitespace.

    These are the only permitted exact-quote normalizations (SPEC 12.4). The
    quote must otherwise match the source verbatim.
    """
    folded = text.replace("\r\n", "\n").replace("\r", "\n").translate(_QUOTE_FOLD)
    return " ".join(folded.split())


def _matches_with_ellipsis(passage: str, quote: str) -> bool:
    """True when ``quote`` matches ``passage`` around ellipsis-marked omissions.

    SPEC 12.4 allows a direct quotation to omit material as long as the
    omission is marked with an ellipsis. Each segment between ellipses must
    still appear verbatim, in order, and without overlapping — so an ellipsis
    can only ever hide text that is genuinely present between the surrounding
    segments. It can never be used to stitch together words the source does
    not contain, nor to reorder them.
    """
    normalized = quote
    for form in _ELLIPSIS_FORMS:
        normalized = normalized.replace(form, "\u2026")
    if "\u2026" not in normalized:
        return False
    segments = [seg.strip() for seg in normalized.split("\u2026")]
    segments = [seg for seg in segments if seg]
    if not segments:
        # A quote consisting only of ellipses asserts nothing; reject it.
        return False
    cursor = 0
    for segment in segments:
        found = passage.find(segment, cursor)
        if found < 0:
            return False
        cursor = found + len(segment)
    return True


def validate_exact_quote(passage: str, exact_quote: str) -> None:
    """Validate that ``exact_quote`` is extractable from the evidence ``passage``.

    Permitted normalizations are exactly those SPEC 12.4 allows: line endings,
    Unicode quote marks, and whitespace collapse. Beyond those the quote must
    be verbatim, so a paraphrase never validates. An omission marked with an
    ellipsis is accepted only when every remaining segment appears in the
    passage verbatim and in order.

    Raises:
        DraftValidationError: ``exact_quote`` is empty or does not occur in
            ``passage`` under those rules.
    """
    if not exact_quote:
        raise DraftValidationError("exact_quote must not be empty")
    if exact_quote in passage:
        return
    normalized_passage = _normalize_quote(passage)
    normalized_quote = _normalize_quote(exact_quote)
    if normalized_quote in normalized_passage:
        return
    if _matches_with_ellipsis(normalized_passage, normalized_quote):
        return
    raise DraftValidationError(
        "exact_quote does not match the snapshotted evidence passage"
    )


# ── Records ──────────────────────────────────────────────────────────────────


@dataclass
class DraftRecord:
    """A Draft Room project row."""

    id: int
    vault_id: int
    created_by: int
    title: str
    mode: str
    status: str
    tier: str
    brief_json: str
    lock_version: int
    ready_revision_id: Optional[int]
    ready_by: Optional[int]
    ready_at: Optional[str]
    archived_at: Optional[str]
    created_at: str
    updated_at: str


@dataclass
class DraftInputRecord:
    """An uploaded project input. ``storage_relpath`` is never exposed over HTTP."""

    id: int
    draft_id: int
    role: str
    authority: str
    as_of_date: Optional[str]
    original_name: str
    stored_name: str
    extension: str
    media_type: Optional[str]
    size_bytes: int
    content_sha256: str
    storage_relpath: str
    parsed_text_sha256: Optional[str]
    parsed_char_count: Optional[int]
    parse_status: str
    parse_error: Optional[str]
    locked_spans_json: str
    created_at: str
    updated_at: str


@dataclass
class DraftJobRecord:
    """A durable Draft Room job."""

    id: int
    draft_id: int
    vault_id: int
    created_by: int
    job_type: str
    input_id: Optional[int]
    parent_job_id: Optional[int]
    attempt_no: int
    idempotency_key: Optional[str]
    status: str
    active_stage: Optional[str]
    start_stage: Optional[str]
    input_revision_id: Optional[int]
    output_revision_id: Optional[int]
    compile_input_sha256: Optional[str]
    prompt_bundle_version: Optional[str]
    retry_count: int
    model_call_count: int
    max_model_calls: int
    timeout_seconds: int
    progress_percent: float
    cancel_requested_at: Optional[str]
    heartbeat_at: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]


@dataclass
class DraftRevisionRecord:
    """An immutable revision row. ``content_md`` is loaded only on detail reads."""

    id: int
    draft_id: int
    parent_revision_id: Optional[int]
    job_id: Optional[int]
    revision_no: int
    source: str
    content_sha256: str
    fact_status: str
    is_current: int
    created_by: Optional[int]
    created_at: str
    content_md: Optional[str] = None
    sections_json: str = "[]"
    citations_json: str = "[]"
    qa_summary_json: str = "{}"


@dataclass
class DraftStageRecord:
    """One immutable attempt of a compile pipeline stage."""

    id: int
    job_id: int
    stage: str
    attempt: int
    status: str
    input_sha256: str
    artifact_json: str
    artifact_sha256: Optional[str]
    content_md: Optional[str]
    candidate_sha256: Optional[str]
    semantic_changed: int
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


@dataclass
class DraftEvidenceRecord:
    """An immutable retrieval-time evidence snapshot for one compile job."""

    id: int
    job_id: int
    label: str
    source_kind: str
    draft_input_id: Optional[int]
    file_id: Optional[int]
    wiki_page_id: Optional[int]
    wiki_claim_id: Optional[int]
    kms_entry_id: Optional[int]
    chunk_uid: Optional[str]
    title: str
    passage: str
    passage_sha256: str
    source_content_sha256: str
    page_number: Optional[int]
    section: Optional[str]
    retrieval_score: Optional[float]
    authority: str
    as_of_date: Optional[str]
    source_updated_at: Optional[str]
    source_deleted_at: Optional[str]
    created_at: str


@dataclass
class DraftEvidenceIdentity:
    """The freshness-relevant projection of one ``draft_evidence`` row.

    Deliberately excludes ``passage``/``title``: SPEC 12.6 re-resolution
    compares identity, hash, and update/delete metadata only, and a bounded
    pass must not drag every snapshotted passage through memory. ``draft_id``
    and ``vault_id`` come from the owning job so a resolver can scope the
    lookup to the draft's vault without a second query.
    """

    id: int
    job_id: int
    draft_id: int
    vault_id: int
    label: str
    source_kind: str
    draft_input_id: Optional[int]
    file_id: Optional[int]
    wiki_page_id: Optional[int]
    wiki_claim_id: Optional[int]
    kms_entry_id: Optional[int]
    source_content_sha256: str
    source_updated_at: Optional[str]
    source_deleted_at: Optional[str]


@dataclass
class DraftClaimRecord:
    """One factual claim extracted from a revision's text."""

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
    retrieval_audit_json: str
    resolution: str
    resolved_by: Optional[int]
    resolved_at: Optional[str]
    resolution_note: Optional[str]


@dataclass
class DraftClaimSourceRecord:
    """One evidence citation attached to a claim.

    ``lexical_overlap_score`` is a purely lexical/statistical diagnostic. It is
    stored separately from — and MUST NOT be conflated with — ``relationship``,
    which is the semantic support/contradiction/context verdict.
    """

    id: int
    claim_id: int
    evidence_id: int
    relationship: str
    exact_quote: str
    passage_start: Optional[int]
    passage_end: Optional[int]
    lexical_overlap_score: Optional[float]


@dataclass
class DraftFindingRecord:
    """An editorial/factuality finding surfaced against a draft."""

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
    waivable: int
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


_DRAFT_COLUMNS = (
    "id, vault_id, created_by, title, mode, status, tier, brief_json, "
    "lock_version, ready_revision_id, ready_by, ready_at, archived_at, "
    "created_at, updated_at"
)
_INPUT_COLUMNS = (
    "id, draft_id, role, authority, as_of_date, original_name, stored_name, "
    "extension, media_type, size_bytes, content_sha256, storage_relpath, "
    "parsed_text_sha256, parsed_char_count, parse_status, parse_error, "
    "locked_spans_json, created_at, updated_at"
)
# The four large compile JSON bodies on ``draft_jobs``. Kept out of
# ``_JOB_COLUMNS`` (see below) and reachable only through
# ``DraftStore.get_job_json_field``/``set_job_json_field``, which validate the
# name against this set before interpolating it into SQL.
_JOB_JSON_FIELDS = frozenset(
    {"input_json", "result_json", "brief_snapshot_json", "model_snapshot_json"}
)

# Scalar compile columns (ids, hashes, versions) are part of the job summary:
# they are cheap and every compile consumer needs them. The large compile JSON
# bodies (input_json, result_json, brief_snapshot_json, model_snapshot_json) are
# deliberately NOT selected here — SPEC §8.2 requires large bodies to be paged or
# fetched explicitly rather than embedded in summaries. Use the dedicated
# ``get_job_*_json`` accessors for those.
_JOB_COLUMNS = (
    "id, draft_id, vault_id, created_by, job_type, input_id, parent_job_id, "
    "attempt_no, idempotency_key, status, active_stage, start_stage, "
    "input_revision_id, output_revision_id, compile_input_sha256, "
    "prompt_bundle_version, "
    "retry_count, model_call_count, max_model_calls, timeout_seconds, "
    "progress_percent, cancel_requested_at, heartbeat_at, error_code, "
    "error_message, created_at, started_at, completed_at"
)
_REVISION_SUMMARY_COLUMNS = (
    "id, draft_id, parent_revision_id, job_id, revision_no, source, "
    "content_sha256, fact_status, is_current, created_by, created_at"
)
_STAGE_COLUMNS = (
    "id, job_id, stage, attempt, status, input_sha256, artifact_json, "
    "artifact_sha256, content_md, candidate_sha256, semantic_changed, "
    "prompt_id, prompt_version, prompt_sha256, model_name, temperature, "
    "input_tokens, output_tokens, error_code, error_message, started_at, "
    "completed_at"
)
_EVIDENCE_COLUMNS = (
    "id, job_id, label, source_kind, draft_input_id, file_id, wiki_page_id, "
    "wiki_claim_id, kms_entry_id, chunk_uid, title, passage, passage_sha256, "
    "source_content_sha256, page_number, section, retrieval_score, authority, "
    "as_of_date, source_updated_at, source_deleted_at, created_at"
)
_EVIDENCE_IDENTITY_COLUMNS = (
    "e.id, e.job_id, j.draft_id, j.vault_id, e.label, e.source_kind, "
    "e.draft_input_id, e.file_id, e.wiki_page_id, e.wiki_claim_id, "
    "e.kms_entry_id, e.source_content_sha256, e.source_updated_at, "
    "e.source_deleted_at"
)
_CLAIM_COLUMNS = (
    "id, revision_id, ordinal, claim_text, claim_sha256, span_start, span_end, "
    "claim_type, status, severity, rationale, retrieval_audit_json, resolution, "
    "resolved_by, resolved_at, resolution_note"
)
_CLAIM_SOURCE_COLUMNS = (
    "id, claim_id, evidence_id, relationship, exact_quote, passage_start, "
    "passage_end, lexical_overlap_score"
)
_FINDING_COLUMNS = (
    "id, draft_id, revision_id, job_id, stage, rule_id, rule_version, category, "
    "severity, status, waivable, message, original_text, suggestion, "
    "span_start, span_end, span_text_sha256, resolved_by, resolved_at, "
    "resolution_note, waiver_rule_version, waiver_text_sha256, created_at"
)


def _row_to_draft(row: sqlite3.Row) -> DraftRecord:
    return DraftRecord(*row)


def _row_to_input(row: sqlite3.Row) -> DraftInputRecord:
    return DraftInputRecord(*row)


def _row_to_job(row: sqlite3.Row) -> DraftJobRecord:
    return DraftJobRecord(*row)


def _row_to_revision_summary(row: sqlite3.Row) -> DraftRevisionRecord:
    return DraftRevisionRecord(*row)


def _row_to_stage(row: sqlite3.Row) -> DraftStageRecord:
    return DraftStageRecord(*row)


def _row_to_evidence(row: sqlite3.Row) -> DraftEvidenceRecord:
    return DraftEvidenceRecord(*row)


def _row_to_evidence_identity(row: sqlite3.Row) -> DraftEvidenceIdentity:
    return DraftEvidenceIdentity(*row)


def _row_to_claim(row: sqlite3.Row) -> DraftClaimRecord:
    return DraftClaimRecord(*row)


def _row_to_claim_source(row: sqlite3.Row) -> DraftClaimSourceRecord:
    return DraftClaimSourceRecord(*row)


def _row_to_finding(row: sqlite3.Row) -> DraftFindingRecord:
    return DraftFindingRecord(*row)


class DraftStore:
    """All Draft Room SQLite access.

    Args:
        db: An open SQLite connection with ``PRAGMA foreign_keys = ON``. The
            caller owns its lifetime; the store never closes it.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row

    # ── transaction helper ───────────────────────────────────────────────

    def _begin_immediate(self) -> None:
        """Take the write lock, clearing any dangling implicit transaction first.

        A prior best-effort statement on this pooled connection may have left an
        implicit transaction open, which would make ``BEGIN IMMEDIATE`` fail with
        "cannot start a transaction within a transaction".
        """
        if self._db.in_transaction:
            self._db.rollback()
        self._db.execute("BEGIN IMMEDIATE")

    # ── drafts ───────────────────────────────────────────────────────────

    def create_draft(
        self,
        *,
        vault_id: int,
        created_by: int,
        title: str,
        mode: str,
        tier: str,
        brief_json: str,
    ) -> DraftRecord:
        """Create an owner-private project and record a ``draft_created`` event.

        Owner and vault are fixed here and can never be changed afterwards —
        there is deliberately no store method that updates either column.
        """
        title = (title or "").strip()
        if not title:
            raise DraftValidationError("title must not be empty")
        if len(title) > _MAX_TITLE_LEN:
            raise DraftValidationError(
                f"title must be at most {_MAX_TITLE_LEN} characters"
            )
        if mode not in DRAFT_MODES:
            raise DraftValidationError(f"unknown draft mode: {mode!r}")
        if tier not in DRAFT_TIERS:
            raise DraftValidationError(f"unknown draft tier: {tier!r}")

        self._begin_immediate()
        try:
            cur = self._db.execute(
                "INSERT INTO drafts (vault_id, created_by, title, mode, tier, brief_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (vault_id, created_by, title, mode, tier, brief_json),
            )
            draft_id = int(cur.lastrowid)
            self._insert_event(
                draft_id=draft_id,
                event_type="draft_created",
                actor_user_id=created_by,
                payload={"mode": mode, "tier": tier, "vault_id": vault_id},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_draft(draft_id, created_by)

    def get_draft(self, draft_id: int, owner_id: int) -> DraftRecord:
        """Load a draft by ``(draft_id, owner_id)``.

        Raises:
            DraftNotFoundError: If it is absent or owned by someone else. The
                two cases are intentionally indistinguishable so draft existence
                is not disclosed across owners.
        """
        row = self._db.execute(
            "SELECT id, vault_id, created_by, title, mode, status, tier, brief_json, "
            "lock_version, ready_revision_id, ready_by, ready_at, archived_at, "
            "created_at, updated_at FROM drafts WHERE id = ? AND created_by = ?",
            (draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("draft not found")
        return _row_to_draft(row)

    def list_drafts(
        self,
        *,
        owner_id: int,
        vault_id: Optional[int] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[DraftRecord], int]:
        """Page through the caller's own drafts, newest first.

        Rows whose vault access has since been revoked are still returned so the
        owner can see and delete their private data (SPEC section 9.1); the route
        annotates each row's current ``vault_access``.

        Returns:
            ``(rows, total)`` ordered by ``updated_at DESC, id DESC`` — the ID
            tie-break keeps paging stable when timestamps collide.
        """
        if status is not None and status not in DRAFT_STATUSES:
            raise DraftValidationError(f"unknown draft status: {status!r}")
        where = ["created_by = ?"]
        params: list[Any] = [owner_id]
        if vault_id is not None:
            where.append("vault_id = ?")
            params.append(vault_id)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = " AND ".join(where)

        # clause is built only from literal fragments; all values are bound parameters
        total = int(
            self._db.execute(
                f"SELECT COUNT(*) FROM drafts WHERE {clause}", params  # nosec B608
            ).fetchone()[0]
        )
        # clause is built only from literal fragments; all values are bound parameters
        rows = self._db.execute(
            f"SELECT {_DRAFT_COLUMNS} FROM drafts WHERE {clause} "  # nosec B608
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, per_page, max(page - 1, 0) * per_page),
        ).fetchall()
        return [_row_to_draft(r) for r in rows], total

    def update_draft(
        self,
        *,
        draft_id: int,
        owner_id: int,
        lock_version: int,
        title: Optional[str] = None,
        brief_json: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> DraftRecord:
        """Update title/brief/tier under an optimistic lock.

        Raises:
            DraftConflictError: If ``lock_version`` is stale or a compile job is
                active (an in-flight job snapshotted the brief it is running on).
        """
        if tier is not None and tier not in DRAFT_TIERS:
            raise DraftValidationError(f"unknown draft tier: {tier!r}")
        if title is not None:
            title = title.strip()
            if not title:
                raise DraftValidationError("title must not be empty")
            if len(title) > _MAX_TITLE_LEN:
                raise DraftValidationError(
                    f"title must be at most {_MAX_TITLE_LEN} characters"
                )

        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, lock_version)
            if draft.status == "archived":
                raise InvalidTransitionError("archived draft cannot be edited")
            self._assert_no_active_compile(draft_id)

            self._db.execute(
                "UPDATE drafts SET title = COALESCE(?, title), "
                "brief_json = COALESCE(?, brief_json), tier = COALESCE(?, tier), "
                "lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND created_by = ?",
                (title, brief_json, tier, draft_id, owner_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_draft(draft_id, owner_id)

    def archive_draft(
        self, *, draft_id: int, owner_id: int, lock_version: int
    ) -> DraftRecord:
        """Archive a project. Input bytes are retained (SPEC section 6.1)."""
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, lock_version)
            _check_transition(
                "draft", draft.status, "archived",
                _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS,
            )
            self._assert_no_active_job(draft_id)
            self._db.execute(
                "UPDATE drafts SET status = 'archived', archived_at = CURRENT_TIMESTAMP, "
                "lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND created_by = ?",
                (draft_id, owner_id),
            )
            self._insert_event(
                draft_id=draft_id,
                event_type="draft_archived",
                actor_user_id=owner_id,
                payload={},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_draft(draft_id, owner_id)

    def restore_draft(
        self, *, draft_id: int, owner_id: int, lock_version: int
    ) -> DraftRecord:
        """Unarchive a project.

        Restores to ``needs_review`` when a current revision exists, otherwise to
        ``draft``. It never restores directly to ``ready`` — only a human action
        on a specific revision may set that.
        """
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, lock_version)
            if draft.status != "archived":
                raise InvalidTransitionError("draft is not archived")
            has_current = (
                self._db.execute(
                    "SELECT 1 FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
                    (draft_id,),
                ).fetchone()
                is not None
            )
            target = "needs_review" if has_current else "draft"
            _check_transition(
                "draft", draft.status, target,
                _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS,
            )
            self._db.execute(
                "UPDATE drafts SET status = ?, archived_at = NULL, "
                "lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND created_by = ?",
                (target, draft_id, owner_id),
            )
            self._insert_event(
                draft_id=draft_id,
                event_type="draft_restored",
                actor_user_id=owner_id,
                payload={"status": target},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_draft(draft_id, owner_id)

    def _locked_draft(
        self, draft_id: int, owner_id: int, lock_version: Optional[int]
    ) -> DraftRecord:
        """Read a draft inside an open write transaction and check its lock."""
        row = self._db.execute(
            "SELECT id, vault_id, created_by, title, mode, status, tier, brief_json, "
            "lock_version, ready_revision_id, ready_by, ready_at, archived_at, "
            "created_at, updated_at FROM drafts WHERE id = ? AND created_by = ?",
            (draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("draft not found")
        draft = _row_to_draft(row)
        if lock_version is not None and draft.lock_version != lock_version:
            raise DraftConflictError(
                "draft was modified by another request; reload and retry"
            )
        return draft

    def _assert_no_active_compile(self, draft_id: int) -> None:
        row = self._db.execute(
            "SELECT id FROM draft_jobs WHERE draft_id = ? AND job_type = 'compile' "
            "AND status IN ('pending','running') LIMIT 1",
            (draft_id,),
        ).fetchone()
        if row is not None:
            raise DraftConflictError("a compile job is active for this draft")

    def _assert_no_active_job(self, draft_id: int) -> None:
        row = self._db.execute(
            "SELECT id FROM draft_jobs WHERE draft_id = ? "
            "AND status IN ('pending','running') LIMIT 1",
            (draft_id,),
        ).fetchone()
        if row is not None:
            raise DraftConflictError("a job is active for this draft")

    def count_active_jobs(self, draft_id: int) -> int:
        """Number of pending/running jobs on a draft (delete/archive guards)."""
        return int(
            self._db.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND status IN ('pending','running')",
                (draft_id,),
            ).fetchone()[0]
        )

    # ── events ───────────────────────────────────────────────────────────

    def _insert_event(
        self,
        *,
        draft_id: int,
        event_type: str,
        actor_user_id: Optional[int] = None,
        job_id: Optional[int] = None,
        revision_id: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append an audit row inside the caller's open transaction.

        ``payload`` is a small JSON object of IDs and reason codes. Never pass
        document content, prompts, paths, or raw exception text — ``draft_events``
        is project history, not a log sink.
        """
        self._db.execute(
            "INSERT INTO draft_events (draft_id, job_id, revision_id, actor_user_id, "
            "event_type, event_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                draft_id,
                job_id,
                revision_id,
                actor_user_id,
                event_type,
                json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
            ),
        )

    def record_event(
        self,
        *,
        draft_id: int,
        owner_id: int,
        event_type: str,
        actor_user_id: Optional[int] = None,
        job_id: Optional[int] = None,
        revision_id: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append and commit a standalone audit event for an owned draft."""
        self.get_draft(draft_id, owner_id)
        self._insert_event(
            draft_id=draft_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            job_id=job_id,
            revision_id=revision_id,
            payload=payload,
        )
        self._db.commit()

    # ── inputs ───────────────────────────────────────────────────────────

    def reserve_input(
        self,
        *,
        draft_id: int,
        owner_id: int,
        role: str,
        authority: str,
        as_of_date: Optional[str],
        original_name: str,
        stored_name: str,
        extension: str,
        media_type: Optional[str],
        size_bytes: int,
        content_sha256: str,
        max_inputs: int,
        max_total_input_bytes: int,
    ) -> DraftInputRecord:
        """Atomically reserve an input row and allocate its storage path.

        Runs in one ``BEGIN IMMEDIATE`` transaction that re-checks ownership,
        the per-project input count and total-bytes limits, and duplicate content
        hash *after* taking the write lock — so two concurrent uploads cannot both
        pass a check-then-act race.

        The relative path is derived from the row's own ``lastrowid`` and is
        therefore unique by construction; the client filename never appears in it.

        Raises:
            DraftNotFoundError: The draft is absent or owned by someone else.
            DraftConflictError: Identical content already exists in this draft.
                The exception carries ``existing_input_id``.
            DraftLimitExceededError: An input-count or total-bytes limit was hit.
        """
        if role not in INPUT_ROLES:
            raise DraftValidationError(f"unknown input role: {role!r}")
        if authority not in INPUT_AUTHORITIES:
            raise DraftValidationError(f"unknown input authority: {authority!r}")
        if size_bytes < 0:
            raise DraftValidationError("size_bytes must not be negative")

        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, None)
            if draft.status == "archived":
                raise InvalidTransitionError(
                    "archived draft cannot accept new inputs"
                )
            self._assert_no_active_compile(draft_id)

            dup = self._db.execute(
                "SELECT id FROM draft_inputs WHERE draft_id = ? AND content_sha256 = ?",
                (draft_id, content_sha256),
            ).fetchone()
            if dup is not None:
                err = DraftConflictError("input content already exists in this draft")
                err.code = "duplicate_input"
                err.existing_input_id = int(dup[0])  # type: ignore[attr-defined]
                raise err

            count_row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM draft_inputs "
                "WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            current_count = int(count_row[0])
            current_bytes = int(count_row[1])
            if current_count >= max_inputs:
                raise DraftLimitExceededError(
                    f"draft already holds the maximum of {max_inputs} inputs"
                )
            if current_bytes + size_bytes > max_total_input_bytes:
                raise DraftLimitExceededError(
                    "draft would exceed its total input size limit"
                )

            # Insert with a temporary unique placeholder: storage_relpath is NOT
            # NULL and UNIQUE, and the real path needs the row id we do not have
            # until after the insert.
            placeholder = f"pending:{draft_id}:{content_sha256}"
            cur = self._db.execute(
                "INSERT INTO draft_inputs (draft_id, role, authority, as_of_date, "
                "original_name, stored_name, extension, media_type, size_bytes, "
                "content_sha256, storage_relpath) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft_id,
                    role,
                    authority,
                    as_of_date,
                    original_name,
                    stored_name,
                    extension,
                    media_type,
                    size_bytes,
                    content_sha256,
                    placeholder,
                ),
            )
            input_id = int(cur.lastrowid)
            relpath = build_input_relpath(
                owner_id=owner_id,
                draft_id=draft_id,
                input_id=input_id,
                stored_name=stored_name,
            )
            self._db.execute(
                "UPDATE draft_inputs SET storage_relpath = ? WHERE id = ?",
                (relpath, input_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_input(draft_id=draft_id, owner_id=owner_id, input_id=input_id)

    def get_input(
        self, *, draft_id: int, owner_id: int, input_id: int
    ) -> DraftInputRecord:
        """Load one input, constrained through its owning draft."""
        row = self._db.execute(
            "SELECT id, draft_id, role, authority, as_of_date, original_name, "
            "stored_name, extension, media_type, size_bytes, content_sha256, "
            "storage_relpath, parsed_text_sha256, parsed_char_count, parse_status, "
            "parse_error, locked_spans_json, created_at, updated_at "
            "FROM draft_inputs i "
            "WHERE i.id = ? AND i.draft_id = ? AND EXISTS ("
            "  SELECT 1 FROM drafts d WHERE d.id = i.draft_id AND d.created_by = ?)",
            (input_id, draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("input not found")
        return _row_to_input(row)

    def list_inputs(self, *, draft_id: int, owner_id: int) -> list[DraftInputRecord]:
        """All inputs for an owned draft, oldest first."""
        self.get_draft(draft_id, owner_id)
        rows = self._db.execute(
            "SELECT id, draft_id, role, authority, as_of_date, original_name, "
            "stored_name, extension, media_type, size_bytes, content_sha256, "
            "storage_relpath, parsed_text_sha256, parsed_char_count, parse_status, "
            "parse_error, locked_spans_json, created_at, updated_at "
            "FROM draft_inputs "
            "WHERE draft_id = ? ORDER BY created_at ASC, id ASC",
            (draft_id,),
        ).fetchall()
        return [_row_to_input(r) for r in rows]

    def get_input_parsed_text(
        self, *, draft_id: int, owner_id: int, input_id: int
    ) -> Optional[str]:
        """Parsed text for one input.

        Kept off :class:`DraftInputRecord` on purpose: parsed text must never
        appear in list or detail responses, only through the dedicated content
        endpoint (SPEC section 8.2).
        """
        self.get_input(draft_id=draft_id, owner_id=owner_id, input_id=input_id)
        row = self._db.execute(
            "SELECT parsed_text FROM draft_inputs WHERE id = ? AND draft_id = ?",
            (input_id, draft_id),
        ).fetchone()
        return None if row is None else row[0]

    def update_input_metadata(
        self,
        *,
        draft_id: int,
        owner_id: int,
        input_id: int,
        role: Optional[str] = None,
        authority: Optional[str] = None,
        as_of_date: Optional[str] = None,
        clear_as_of_date: bool = False,
        locked_spans_json: Optional[str] = None,
    ) -> DraftInputRecord:
        """Update an input's role/authority/as-of date/locked spans.

        Not permitted while a compile job is active: a running job snapshotted
        these values and must not observe them change underneath it.
        """
        if role is not None and role not in INPUT_ROLES:
            raise DraftValidationError(f"unknown input role: {role!r}")
        if authority is not None and authority not in INPUT_AUTHORITIES:
            raise DraftValidationError(f"unknown input authority: {authority!r}")

        self._begin_immediate()
        try:
            self._locked_draft(draft_id, owner_id, None)
            self._assert_no_active_compile(draft_id)
            existing = self._db.execute(
                "SELECT id FROM draft_inputs WHERE id = ? AND draft_id = ?",
                (input_id, draft_id),
            ).fetchone()
            if existing is None:
                raise DraftNotFoundError("input not found")

            if clear_as_of_date:
                self._db.execute(
                    "UPDATE draft_inputs SET as_of_date = NULL WHERE id = ?",
                    (input_id,),
                )
            self._db.execute(
                "UPDATE draft_inputs SET role = COALESCE(?, role), "
                "authority = COALESCE(?, authority), "
                "as_of_date = COALESCE(?, as_of_date), "
                "locked_spans_json = COALESCE(?, locked_spans_json), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND draft_id = ?",
                (role, authority, as_of_date, locked_spans_json, input_id, draft_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_input(draft_id=draft_id, owner_id=owner_id, input_id=input_id)

    def set_input_parse_status(
        self,
        *,
        input_id: int,
        target: str,
        parsed_text: Optional[str] = None,
        parsed_text_sha256: Optional[str] = None,
        parsed_char_count: Optional[int] = None,
        parse_error: Optional[str] = None,
        allow_recovery: bool = False,
    ) -> None:
        """Move an input through its parse state machine and commit.

        Args:
            target: The new ``parse_status``; validated against the transition
                table so no caller can write an arbitrary string.
            allow_recovery: Only startup orphan recovery may pass True, which
                additionally permits ``parsing -> pending``.

        Raises:
            DraftNotFoundError: The input row is gone.
            InvalidTransitionError: The transition is not allowed.
        """
        if target not in INPUT_STATUSES:
            raise DraftValidationError(f"unknown input parse status: {target!r}")
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT parse_status FROM draft_inputs WHERE id = ?", (input_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("input not found")
            _check_transition(
                "input", row[0], target,
                _INPUT_TRANSITIONS, _INPUT_RECOVERY_TRANSITIONS,
                allow_recovery=allow_recovery,
            )
            self._db.execute(
                "UPDATE draft_inputs SET parse_status = ?, "
                "parsed_text = COALESCE(?, parsed_text), "
                "parsed_text_sha256 = COALESCE(?, parsed_text_sha256), "
                "parsed_char_count = COALESCE(?, parsed_char_count), "
                "parse_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    target,
                    parsed_text,
                    parsed_text_sha256,
                    parsed_char_count,
                    _truncate(parse_error, _MAX_ERROR_MESSAGE_LEN),
                    input_id,
                ),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def total_parsed_chars(self, draft_id: int, *, excluding_input_id: int) -> int:
        """Sum of parsed characters across a draft's ready inputs.

        ``excluding_input_id`` omits the input currently being parsed so its own
        pending contribution is not double counted by the caller.
        """
        row = self._db.execute(
            "SELECT COALESCE(SUM(parsed_char_count), 0) FROM draft_inputs "
            "WHERE draft_id = ? AND id != ? AND parse_status = 'ready'",
            (draft_id, excluding_input_id),
        ).fetchone()
        return int(row[0])

    def delete_input_row(
        self, *, draft_id: int, owner_id: int, input_id: int
    ) -> None:
        """Delete an input row (phase 2 of the tombstone flow) and audit it.

        The file is renamed into ``.trash`` *before* this runs and is restored if
        this transaction fails, so bytes and rows can never disagree.
        """
        self._begin_immediate()
        try:
            self._locked_draft(draft_id, owner_id, None)
            cur = self._db.execute(
                "DELETE FROM draft_inputs WHERE id = ? AND draft_id = ?",
                (input_id, draft_id),
            )
            if cur.rowcount == 0:
                raise DraftNotFoundError("input not found")
            self._insert_event(
                draft_id=draft_id,
                event_type="input_deleted",
                actor_user_id=owner_id,
                payload={"input_id": input_id},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def input_is_in_use(self, *, draft_id: int, input_id: int) -> bool:
        """True when a completed job derived durable artifacts from this input.

        A used input cannot be deleted piecemeal (``409 input_in_use``): immutable
        revisions contain derivatives, and pretending otherwise would be dishonest.
        Whole-draft deletion is the comprehensive purge.
        """
        row = self._db.execute(
            "SELECT 1 FROM draft_jobs WHERE draft_id = ? AND input_id = ? "
            "AND job_type = 'compile' AND status = 'completed' LIMIT 1",
            (draft_id, input_id),
        ).fetchone()
        if row is not None:
            return True
        row = self._db.execute(
            "SELECT 1 FROM draft_revisions r JOIN draft_jobs j ON j.id = r.job_id "
            "WHERE r.draft_id = ? AND j.input_id = ? LIMIT 1",
            (draft_id, input_id),
        ).fetchone()
        return row is not None

    # ── revisions ────────────────────────────────────────────────────────

    def create_manual_revision(
        self,
        *,
        draft_id: int,
        owner_id: int,
        lock_version: int,
        base_revision_id: Optional[int],
        content_md: str,
    ) -> DraftRevisionRecord:
        """Save an immutable manual Markdown revision.

        The whole allocation runs in one ``BEGIN IMMEDIATE`` transaction, exactly
        as SPEC section 5.3 requires: clear the previous current flag, allocate
        ``MAX(revision_no) + 1``, insert the immutable row, mark it current, move
        the project to ``needs_review``, clear Ready state, and bump the lock.

        ``base_revision_id`` must name the draft's current revision, or be null
        when the draft has none — this is what makes a concurrent edit fail loudly
        instead of silently forking history.
        """
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, lock_version)
            if draft.status == "archived":
                raise InvalidTransitionError(
                    "archived draft cannot accept new revisions"
                )
            self._assert_no_active_compile(draft_id)

            current = self._db.execute(
                "SELECT id FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
                (draft_id,),
            ).fetchone()
            current_id = None if current is None else int(current[0])
            if base_revision_id != current_id:
                raise DraftConflictError(
                    "base_revision_id does not match the draft's current revision"
                )

            next_no = int(
                self._db.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM draft_revisions "
                    "WHERE draft_id = ?",
                    (draft_id,),
                ).fetchone()[0]
            )
            # Clear the old current flag first: the partial unique index allows
            # only one is_current=1 row per draft at any instant.
            if current_id is not None:
                self._db.execute(
                    "UPDATE draft_revisions SET is_current = 0 WHERE id = ?",
                    (current_id,),
                )
            cur = self._db.execute(
                "INSERT INTO draft_revisions (draft_id, parent_revision_id, job_id, "
                "revision_no, source, content_md, content_sha256, fact_status, "
                "is_current, created_by) "
                "VALUES (?, ?, NULL, ?, 'manual', ?, ?, 'not_run', 1, ?)",
                (
                    draft_id,
                    current_id,
                    next_no,
                    content_md,
                    sha256_text(content_md),
                    owner_id,
                ),
            )
            revision_id = int(cur.lastrowid)

            # A manual edit is a semantic change: any prior Ready approval and
            # factual result no longer describe the current text.
            target_status = "needs_review"
            if draft.status not in ("needs_review",):
                _check_transition(
                    "draft", draft.status, target_status,
                    _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS,
                )
            self._db.execute(
                "UPDATE drafts SET status = ?, ready_revision_id = NULL, "
                "ready_by = NULL, ready_at = NULL, lock_version = lock_version + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND created_by = ?",
                (target_status, draft_id, owner_id),
            )
            self._insert_event(
                draft_id=draft_id,
                event_type="revision_created",
                actor_user_id=owner_id,
                revision_id=revision_id,
                payload={"source": "manual", "revision_no": next_no},
            )
            if draft.ready_revision_id is not None:
                self._insert_event(
                    draft_id=draft_id,
                    event_type="ready_invalidated",
                    actor_user_id=owner_id,
                    revision_id=revision_id,
                    payload={"reason": "manual_revision"},
                )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_revision(
            draft_id=draft_id, owner_id=owner_id, revision_id=revision_id
        )

    def get_revision(
        self,
        *,
        draft_id: int,
        owner_id: int,
        revision_id: int,
        include_content: bool = True,
    ) -> DraftRevisionRecord:
        """Load one immutable revision, constrained through its owning draft."""
        if include_content:
            query = (
                "SELECT id, draft_id, parent_revision_id, job_id, revision_no, "
                "source, content_sha256, fact_status, is_current, created_by, "
                "created_at, content_md, sections_json, citations_json, "
                "qa_summary_json FROM draft_revisions r "
                "WHERE r.id = ? AND r.draft_id = ? AND EXISTS ("
                "  SELECT 1 FROM drafts d WHERE d.id = r.draft_id AND d.created_by = ?)"
            )
        else:
            query = (
                "SELECT id, draft_id, parent_revision_id, job_id, revision_no, "
                "source, content_sha256, fact_status, is_current, created_by, "
                "created_at FROM draft_revisions r "
                "WHERE r.id = ? AND r.draft_id = ? AND EXISTS ("
                "  SELECT 1 FROM drafts d WHERE d.id = r.draft_id AND d.created_by = ?)"
            )
        row = self._db.execute(
            query,
            (revision_id, draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("revision not found")
        return _row_to_revision_summary(row)

    def get_current_revision(
        self, *, draft_id: int, owner_id: int
    ) -> Optional[DraftRevisionRecord]:
        """The draft's current revision summary, or None when it has none."""
        self.get_draft(draft_id, owner_id)
        row = self._db.execute(
            "SELECT id, draft_id, parent_revision_id, job_id, revision_no, source, "
            "content_sha256, fact_status, is_current, created_by, created_at "
            "FROM draft_revisions "
            "WHERE draft_id = ? AND is_current = 1",
            (draft_id,),
        ).fetchone()
        return None if row is None else _row_to_revision_summary(row)

    def list_revisions(
        self, *, draft_id: int, owner_id: int, page: int = 1, per_page: int = 50
    ) -> tuple[list[DraftRevisionRecord], int]:
        """Page through revision metadata, newest first. Never returns Markdown."""
        self.get_draft(draft_id, owner_id)
        total = int(
            self._db.execute(
                "SELECT COUNT(*) FROM draft_revisions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()[0]
        )
        rows = self._db.execute(
            "SELECT id, draft_id, parent_revision_id, job_id, revision_no, source, "
            "content_sha256, fact_status, is_current, created_by, created_at "
            "FROM draft_revisions "
            "WHERE draft_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (draft_id, per_page, max(page - 1, 0) * per_page),
        ).fetchall()
        return [_row_to_revision_summary(r) for r in rows], total

    def count_revisions(self, draft_id: int) -> int:
        """Number of revisions on a draft."""
        return int(
            self._db.execute(
                "SELECT COUNT(*) FROM draft_revisions WHERE draft_id = ?", (draft_id,)
            ).fetchone()[0]
        )

    # ── jobs ─────────────────────────────────────────────────────────────

    def enqueue_parse_job(
        self, *, draft_id: int, owner_id: int, input_id: int, timeout_seconds: int
    ) -> DraftJobRecord:
        """Enqueue the single durable ``parse_input`` job for an input.

        ``max_model_calls`` is fixed at 0: parsing never calls a model. The
        partial unique index guarantees at most one active parse job per input;
        hitting it surfaces as a conflict rather than a 500.
        """
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, None)
            row = self._db.execute(
                "SELECT id FROM draft_inputs WHERE id = ? AND draft_id = ?",
                (input_id, draft_id),
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("input not found")
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                    "input_id, max_model_calls, timeout_seconds) "
                    "VALUES (?, ?, ?, 'parse_input', ?, 0, ?)",
                    (draft_id, draft.vault_id, draft.created_by, input_id, timeout_seconds),
                )
            except sqlite3.IntegrityError as exc:
                raise DraftConflictError(
                    "a parse job is already active for this input"
                ) from exc
            job_id = int(cur.lastrowid)
            self._insert_event(
                draft_id=draft_id,
                event_type="input_uploaded",
                actor_user_id=owner_id,
                job_id=job_id,
                payload={"input_id": input_id},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_job(draft_id=draft_id, owner_id=owner_id, job_id=job_id)

    def get_job(self, *, draft_id: int, owner_id: int, job_id: int) -> DraftJobRecord:
        """Load one job, constrained through its owning draft."""
        row = self._db.execute(
            f"SELECT {_JOB_COLUMNS} "  # nosec B608
            "FROM draft_jobs j "
            "WHERE j.id = ? AND j.draft_id = ? AND EXISTS ("
            "  SELECT 1 FROM drafts d WHERE d.id = j.draft_id AND d.created_by = ?)",
            (job_id, draft_id, owner_id),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("job not found")
        return _row_to_job(row)

    def list_jobs(
        self,
        *,
        draft_id: int,
        owner_id: int,
        job_type: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[DraftJobRecord], int]:
        """Page through a draft's job history, newest first."""
        self.get_draft(draft_id, owner_id)
        if job_type is not None and job_type not in JOB_TYPES:
            raise DraftValidationError(f"unknown job type: {job_type!r}")
        if status is not None and status not in JOB_STATUSES:
            raise DraftValidationError(f"unknown job status: {status!r}")
        where = ["draft_id = ?"]
        params: list[Any] = [draft_id]
        if job_type is not None:
            where.append("job_type = ?")
            params.append(job_type)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = " AND ".join(where)
        # clause is built only from literal fragments; all values are bound parameters
        total = int(
            self._db.execute(
                f"SELECT COUNT(*) FROM draft_jobs WHERE {clause}", params  # nosec B608
            ).fetchone()[0]
        )
        # clause is built only from literal fragments; all values are bound parameters
        rows = self._db.execute(
            f"SELECT {_JOB_COLUMNS} FROM draft_jobs WHERE {clause} "  # nosec B608
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, per_page, max(page - 1, 0) * per_page),
        ).fetchall()
        return [_row_to_job(r) for r in rows], total

    def get_active_parse_job_id(self, input_id: int) -> Optional[int]:
        """ID of the input's active parse job, if any."""
        row = self._db.execute(
            "SELECT id FROM draft_jobs WHERE input_id = ? AND job_type = 'parse_input' "
            "AND status IN ('pending','running') ORDER BY id DESC LIMIT 1",
            (input_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def get_last_parse_job_id(self, input_id: int) -> Optional[int]:
        """ID of the input's most recent parse job in any status."""
        row = self._db.execute(
            "SELECT id FROM draft_jobs WHERE input_id = ? AND job_type = 'parse_input' "
            "ORDER BY id DESC LIMIT 1",
            (input_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def claim_next_parse_job(self) -> Optional[DraftJobRecord]:
        """Atomically claim one pending ``parse_input`` job.

        Takes the write lock with ``BEGIN IMMEDIATE`` and only flips
        ``pending -> running`` while the status is *still* pending, so a second
        processor racing for the same row claims nothing rather than double-running
        it. Returns None when the queue is empty.
        """
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT id FROM draft_jobs WHERE status = 'pending' "
                "AND job_type = 'parse_input' ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                self._db.rollback()
                return None
            job_id = int(row[0])
            cur = self._db.execute(
                "UPDATE draft_jobs SET status = 'running', "
                "started_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'pending'",
                (job_id,),
            )
            if cur.rowcount == 0:
                self._db.rollback()
                return None
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        row = self._db.execute(
            f"SELECT {_JOB_COLUMNS} FROM draft_jobs WHERE id = ?",  # nosec B608
            (job_id,),
        ).fetchone()
        return None if row is None else _row_to_job(row)

    def set_job_status(
        self,
        *,
        job_id: int,
        target: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        progress_percent: Optional[float] = None,
        allow_recovery: bool = False,
    ) -> None:
        """Move a job through its state machine and commit.

        Args:
            target: New status, validated against the transition table.
            error_code: Stable snake_case failure code (never raw exception text).
            error_message: Bounded, redacted diagnostic. Truncated on write.
            allow_recovery: Only startup recovery may pass True, permitting
                ``running -> pending``.
        """
        if target not in JOB_STATUSES:
            raise DraftValidationError(f"unknown job status: {target!r}")
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT status FROM draft_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("job not found")
            _check_transition(
                "job", row[0], target,
                _JOB_TRANSITIONS, _JOB_RECOVERY_TRANSITIONS,
                allow_recovery=allow_recovery,
            )
            terminal = target in ("completed", "failed", "cancelled")
            self._db.execute(
                "UPDATE draft_jobs SET status = ?, error_code = ?, error_message = ?, "
                "progress_percent = COALESCE(?, progress_percent), "
                "heartbeat_at = CURRENT_TIMESTAMP, "
                "started_at = CASE WHEN ? = 'pending' THEN NULL ELSE started_at END, "
                "completed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE completed_at END "
                "WHERE id = ?",
                (
                    target,
                    error_code,
                    _truncate(error_message, _MAX_ERROR_MESSAGE_LEN),
                    progress_percent,
                    target,
                    1 if terminal else 0,
                    job_id,
                ),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def request_job_cancel(
        self, *, draft_id: int, owner_id: int, job_id: int
    ) -> DraftJobRecord:
        """Cancel a job.

        A pending job goes straight to ``cancelled``. A running job gets a
        cancellation request that the worker observes cooperatively before it
        commits any output. Cancelling is allowed even after vault access is
        revoked, because it only reduces private processing.

        Raises:
            DraftConflictError: The job already reached a terminal state.
        """
        self._begin_immediate()
        try:
            self._locked_draft(draft_id, owner_id, None)
            row = self._db.execute(
                "SELECT status, job_type FROM draft_jobs WHERE id = ? AND draft_id = ?",
                (job_id, draft_id),
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("job not found")
            status, job_type = row[0], row[1]
            if status in ("completed", "failed", "cancelled"):
                raise DraftConflictError("job has already finished")
            if status == "pending":
                _check_transition(
                    "job", status, "cancelled",
                    _JOB_TRANSITIONS, _JOB_RECOVERY_TRANSITIONS,
                )
                self._db.execute(
                    "UPDATE draft_jobs SET status = 'cancelled', "
                    "cancel_requested_at = CURRENT_TIMESTAMP, "
                    "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (job_id,),
                )
                # A pending compile job is terminated here rather than by the
                # worker, so nothing else will settle the draft. Enqueue moved
                # it to 'queued', and 'queued' may only exit to running/failed/
                # cancelled -- none of which can still happen once the job is
                # terminal. Without this the draft is permanently stuck and no
                # HTTP path can compile it again, because 'queued' is not a
                # compilable status. A running job needs no equivalent: the
                # pipeline settles the draft itself.
                if job_type == "compile":
                    draft_status = self._db.execute(
                        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
                    ).fetchone()
                    if draft_status is not None and draft_status[0] == "queued":
                        _check_transition(
                            "draft", "queued", "cancelled",
                            _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS,
                        )
                        self._db.execute(
                            "UPDATE drafts SET status = 'cancelled', "
                            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (draft_id,),
                        )
            else:
                self._db.execute(
                    "UPDATE draft_jobs SET cancel_requested_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND cancel_requested_at IS NULL",
                    (job_id,),
                )
            self._insert_event(
                draft_id=draft_id,
                event_type="job_cancelled",
                actor_user_id=owner_id,
                job_id=job_id,
                payload={"prior_status": status},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_job(draft_id=draft_id, owner_id=owner_id, job_id=job_id)

    def is_cancel_requested(self, job_id: int) -> bool:
        """True once a cancellation has been requested for this job."""
        row = self._db.execute(
            "SELECT cancel_requested_at FROM draft_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return bool(row is not None and row[0] is not None)

    def retry_parse_job(
        self, *, draft_id: int, owner_id: int, job_id: int, timeout_seconds: int
    ) -> DraftJobRecord:
        """Create a child retry job for a failed/cancelled parse job.

        The terminal job is preserved for audit and is never mutated back to
        pending; the retry is a new row carrying ``parent_job_id`` and
        ``attempt_no + 1``, and the input returns to ``pending``.
        """
        self._begin_immediate()
        try:
            draft = self._locked_draft(draft_id, owner_id, None)
            row = self._db.execute(
                "SELECT status, job_type, input_id, attempt_no FROM draft_jobs "
                "WHERE id = ? AND draft_id = ?",
                (job_id, draft_id),
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("job not found")
            status, job_type, input_id, attempt_no = row[0], row[1], row[2], int(row[3])
            if job_type != "parse_input":
                raise DraftValidationError("only parse jobs can be retried in this release")
            if status not in ("failed", "cancelled"):
                raise DraftConflictError("only a failed or cancelled job can be retried")
            if input_id is None:
                raise DraftValidationError("parse job has no input")

            input_row = self._db.execute(
                "SELECT parse_status FROM draft_inputs WHERE id = ? AND draft_id = ?",
                (input_id, draft_id),
            ).fetchone()
            if input_row is None:
                raise DraftNotFoundError("input not found")
            _check_transition(
                "input", input_row[0], "pending",
                _INPUT_TRANSITIONS, _INPUT_RECOVERY_TRANSITIONS,
            )
            self._db.execute(
                "UPDATE draft_inputs SET parse_status = 'pending', parse_error = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (input_id,),
            )
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                    "input_id, parent_job_id, attempt_no, max_model_calls, timeout_seconds) "
                    "VALUES (?, ?, ?, 'parse_input', ?, ?, ?, 0, ?)",
                    (
                        draft_id,
                        draft.vault_id,
                        draft.created_by,
                        input_id,
                        job_id,
                        attempt_no + 1,
                        timeout_seconds,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DraftConflictError(
                    "a parse job is already active for this input"
                ) from exc
            new_job_id = int(cur.lastrowid)
            self._insert_event(
                draft_id=draft_id,
                event_type="job_retried",
                actor_user_id=owner_id,
                job_id=new_job_id,
                payload={"input_id": input_id, "parent_job_id": job_id},
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get_job(draft_id=draft_id, owner_id=owner_id, job_id=new_job_id)

    # ── startup recovery ─────────────────────────────────────────────────

    def recover_orphaned_parse_jobs(self) -> int:
        """Return crashed ``running`` parse jobs to ``pending`` at startup.

        A process that dies mid-parse leaves the job ``running`` and its input
        ``parsing`` forever. This resets both, marking the abandoned attempt with
        ``worker_restart`` so the audit trail shows why. Jobs whose cancellation
        was already requested go to ``cancelled`` instead of being resurrected.

        Returns:
            The number of jobs reset to pending.
        """
        self._begin_immediate()
        try:
            rows = self._db.execute(
                "SELECT j.id, j.input_id, j.cancel_requested_at, i.parse_status "
                "FROM draft_jobs j "
                "LEFT JOIN draft_inputs i ON i.id = j.input_id "
                "WHERE j.status = 'running' AND j.job_type = 'parse_input'"
            ).fetchall()
            reset = 0
            settled = 0
            for row in rows:
                job_id, input_id, cancel_requested_at = row[0], row[1], row[2]
                parse_status = row[3]
                settled_status = _TERMINAL_PARSE_TO_JOB_STATUS.get(parse_status)
                if settled_status is not None:
                    # The parse itself committed; only the job's own status
                    # write was lost. Settle the job onto that outcome rather
                    # than re-running work whose result is already stored.
                    if settled_status == "completed":
                        self._db.execute(
                            "UPDATE draft_jobs SET status = 'completed', "
                            "progress_percent = 100.0, "
                            "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (job_id,),
                        )
                    else:
                        # Leave progress_percent alone: whatever the worker had
                        # recorded before dying is the truthful last value, and
                        # the input row already carries the parse_error detail.
                        self._db.execute(
                            "UPDATE draft_jobs SET status = ?, "
                            "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (settled_status, job_id),
                        )
                    settled += 1
                    continue
                if cancel_requested_at is not None:
                    self._db.execute(
                        "UPDATE draft_jobs SET status = 'cancelled', "
                        "error_code = 'worker_restart', "
                        "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (job_id,),
                    )
                    if input_id is not None:
                        self._db.execute(
                            "UPDATE draft_inputs SET parse_status = 'cancelled', "
                            "updated_at = CURRENT_TIMESTAMP "
                            "WHERE id = ? AND parse_status = 'parsing'",
                            (input_id,),
                        )
                    continue
                self._db.execute(
                    "UPDATE draft_jobs SET status = 'pending', started_at = NULL, "
                    "error_code = 'worker_restart', error_message = "
                    "'previous attempt abandoned by a worker restart' WHERE id = ?",
                    (job_id,),
                )
                if input_id is not None:
                    self._db.execute(
                        "UPDATE draft_inputs SET parse_status = 'pending', "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND parse_status = 'parsing'",
                        (input_id,),
                    )
                reset += 1
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        if reset:
            logger.warning(
                "draft room: reset %d orphaned parse job(s) to pending after restart",
                reset,
            )
        if settled:
            logger.warning(
                "draft room: settled %d orphaned parse job(s) onto their input's "
                "already-committed outcome after restart",
                settled,
            )
        return reset

    def list_pending_inputs_without_active_job(self) -> list[tuple[int, int, int, str]]:
        """Inputs stuck ``pending`` with no active parse job.

        A crash between the reservation commit and the job enqueue leaves an input
        that would otherwise never be parsed. Startup reconciliation re-enqueues
        those whose bytes are present and fails those whose bytes are missing.

        Returns:
            Tuples of ``(input_id, draft_id, owner_id, storage_relpath)``.
        """
        rows = self._db.execute(
            "SELECT i.id, i.draft_id, d.created_by, i.storage_relpath "
            "FROM draft_inputs i JOIN drafts d ON d.id = i.draft_id "
            "WHERE i.parse_status = 'pending' AND NOT EXISTS ("
            "  SELECT 1 FROM draft_jobs j WHERE j.input_id = i.id "
            "  AND j.job_type = 'parse_input' AND j.status IN ('pending','running'))"
        ).fetchall()
        return [(int(r[0]), int(r[1]), int(r[2]), r[3]) for r in rows]

    def enqueue_parse_job_for_recovery(
        self, *, input_id: int, timeout_seconds: int
    ) -> Optional[int]:
        """Re-enqueue a parse job for an orphaned input during startup recovery.

        Returns the new job ID, or None when a job already exists (another
        process won the race) or the input has since disappeared.
        """
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT i.draft_id, d.vault_id, d.created_by FROM draft_inputs i "
                "JOIN drafts d ON d.id = i.draft_id WHERE i.id = ?",
                (input_id,),
            ).fetchone()
            if row is None:
                self._db.rollback()
                return None
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                    "input_id, max_model_calls, timeout_seconds) "
                    "VALUES (?, ?, ?, 'parse_input', ?, 0, ?)",
                    (int(row[0]), int(row[1]), int(row[2]), input_id, timeout_seconds),
                )
            except sqlite3.IntegrityError:
                self._db.rollback()
                return None
            job_id = int(cur.lastrowid)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return job_id

    # ── deletion support ─────────────────────────────────────────────────

    def list_input_relpaths(self, draft_id: int) -> list[str]:
        """Every stored relative path belonging to a draft (deletion planning)."""
        rows = self._db.execute(
            "SELECT storage_relpath FROM draft_inputs WHERE draft_id = ?",
            (draft_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def delete_draft_row(self, *, draft_id: int, owner_id: int) -> None:
        """Delete the project row; children cascade via foreign keys.

        Phase 2 of the whole-draft tombstone flow. Inputs, revisions, jobs, and
        project events all carry ``ON DELETE CASCADE`` to ``drafts``, so this one
        statement purges the derivative content. The content-free security-audit
        deletion event is recorded separately by the caller and survives.
        """
        self._begin_immediate()
        try:
            cur = self._db.execute(
                "DELETE FROM drafts WHERE id = ? AND created_by = ?",
                (draft_id, owner_id),
            )
            if cur.rowcount == 0:
                raise DraftNotFoundError("draft not found")
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def cancel_pending_jobs(self, *, draft_id: int, owner_id: int) -> int:
        """Cancel every pending job on a draft before deleting it.

        Running jobs are not force-terminated here: the delete route returns 409
        until the owner cancels them, so a worker can never write into rows that
        are being removed underneath it.

        Returns:
            The number of pending jobs cancelled.
        """
        self._begin_immediate()
        try:
            self._locked_draft(draft_id, owner_id, None)
            cur = self._db.execute(
                "UPDATE draft_jobs SET status = 'cancelled', "
                "cancel_requested_at = CURRENT_TIMESTAMP, "
                "completed_at = CURRENT_TIMESTAMP "
                "WHERE draft_id = ? AND status = 'pending'",
                (draft_id,),
            )
            count = cur.rowcount
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return count

    def list_draft_ids_for_vault(self, vault_id: int) -> list[tuple[int, int]]:
        """``(draft_id, owner_id)`` for every draft in a vault (cascade cleanup)."""
        rows = self._db.execute(
            "SELECT id, created_by FROM drafts WHERE vault_id = ?", (vault_id,)
        ).fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]

    def list_draft_ids_for_user(self, user_id: int) -> list[tuple[int, int]]:
        """``(draft_id, owner_id)`` for every draft owned by a user."""
        rows = self._db.execute(
            "SELECT id, created_by FROM drafts WHERE created_by = ?", (user_id,)
        ).fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]

    def list_all_owner_draft_pairs(self) -> set[tuple[int, int]]:
        """Every ``(owner_id, draft_id)`` pair that currently exists.

        Startup filesystem reconciliation compares this against the directories
        under ``data/draft-room/`` so orphaned private bytes can be removed.
        """
        rows = self._db.execute("SELECT created_by, id FROM drafts").fetchall()
        return {(int(r[0]), int(r[1])) for r in rows}

    # ── pipeline stage artifacts ─────────────────────────────────────────

    def record_stage_start(
        self, *, job_id: int, stage: str, attempt: int, input_sha256: str
    ) -> int:
        """Insert a new running stage-attempt row.

        Raises:
            DraftValidationError: ``stage`` is not a known stage name.
            DraftConflictError: ``(job_id, stage, attempt)`` already exists —
                stage artifacts are immutable per attempt; a retry must use a
                new ``attempt`` number, never overwrite a recorded one.
        """
        if stage not in STAGE_NAMES:
            raise DraftValidationError(f"unknown stage: {stage!r}")
        self._begin_immediate()
        try:
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_job_stages (job_id, stage, attempt, status, "
                    "input_sha256, started_at) "
                    "VALUES (?, ?, ?, 'running', ?, CURRENT_TIMESTAMP)",
                    (job_id, stage, attempt, input_sha256),
                )
            except sqlite3.IntegrityError as exc:
                raise DraftConflictError(
                    f"stage attempt already recorded for job {job_id}, "
                    f"stage {stage!r}, attempt {attempt}"
                ) from exc
            stage_row_id = int(cur.lastrowid)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return stage_row_id

    def record_stage_success(
        self,
        *,
        stage_row_id: int,
        artifact_json: str,
        artifact_sha256: str,
        content_md: Optional[str] = None,
        candidate_sha256: Optional[str] = None,
        semantic_changed: bool = False,
        prompt_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        prompt_sha256: Optional[str] = None,
        model_name: Optional[str] = None,
        temperature: Optional[float] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> None:
        """Complete a stage attempt with its canonical artifact.

        Raises:
            DraftNotFoundError: The stage-attempt row does not exist.
            DraftConflictError: The row already completed — a completed stage
                attempt's artifact is immutable; a correction requires a new
                attempt, never an update.
            DraftValidationError: ``artifact_sha256``/``candidate_sha256`` does
                not match the hash of the payload actually supplied.
        """
        if artifact_sha256 != sha256_text(artifact_json):
            raise DraftValidationError(
                "artifact_sha256 does not match the hash of artifact_json"
            )
        if content_md is not None and candidate_sha256 != sha256_text(content_md):
            raise DraftValidationError(
                "candidate_sha256 does not match the hash of content_md"
            )
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT status FROM draft_job_stages WHERE id = ?", (stage_row_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("stage attempt not found")
            if row[0] == "completed":
                raise DraftConflictError(
                    "stage attempt is already completed and immutable"
                )
            _check_transition(
                "stage", row[0], "completed",
                _STAGE_TRANSITIONS, _STAGE_RECOVERY_TRANSITIONS,
            )
            cur = self._db.execute(
                "UPDATE draft_job_stages SET status = 'completed', "
                "artifact_json = ?, artifact_sha256 = ?, content_md = ?, "
                "candidate_sha256 = ?, semantic_changed = ?, prompt_id = ?, "
                "prompt_version = ?, prompt_sha256 = ?, model_name = ?, "
                "temperature = ?, input_tokens = ?, output_tokens = ?, "
                "completed_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status != 'completed'",
                (
                    artifact_json,
                    artifact_sha256,
                    content_md,
                    candidate_sha256,
                    1 if semantic_changed else 0,
                    prompt_id,
                    prompt_version,
                    prompt_sha256,
                    model_name,
                    temperature,
                    input_tokens,
                    output_tokens,
                    stage_row_id,
                ),
            )
            if cur.rowcount == 0:
                raise DraftConflictError(
                    "stage attempt is already completed and immutable"
                )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def record_stage_failure(
        self,
        *,
        stage_row_id: int,
        error_code: str,
        error_message: Optional[str] = None,
        target: str = "failed",
    ) -> None:
        """Mark a stage attempt ``failed``, ``skipped``, or ``cancelled``.

        Raises:
            DraftValidationError: ``target`` is not a valid terminal status.
            DraftNotFoundError: The stage-attempt row does not exist.
            DraftConflictError: The row already completed and is immutable.
        """
        if target not in ("failed", "skipped", "cancelled"):
            raise DraftValidationError(f"invalid terminal stage status: {target!r}")
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT status FROM draft_job_stages WHERE id = ?", (stage_row_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("stage attempt not found")
            if row[0] == "completed":
                raise DraftConflictError(
                    "stage attempt is already completed and immutable"
                )
            _check_transition(
                "stage", row[0], target,
                _STAGE_TRANSITIONS, _STAGE_RECOVERY_TRANSITIONS,
            )
            self._db.execute(
                "UPDATE draft_job_stages SET status = ?, error_code = ?, "
                "error_message = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    target,
                    error_code,
                    _truncate(error_message, _MAX_ERROR_MESSAGE_LEN),
                    stage_row_id,
                ),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def get_stage(
        self, *, job_id: int, stage: str, attempt: int
    ) -> Optional[DraftStageRecord]:
        """Load one stage attempt, or None if it has not been recorded yet."""
        row = self._db.execute(
            f"SELECT {_STAGE_COLUMNS} FROM draft_job_stages "  # nosec B608
            "WHERE job_id = ? AND stage = ? AND attempt = ?",
            (job_id, stage, attempt),
        ).fetchone()
        return None if row is None else _row_to_stage(row)

    def list_stages(
        self, *, job_id: int, limit: int = 100, offset: int = 0
    ) -> list[DraftStageRecord]:
        """Page through a job's recorded stage attempts, oldest first."""
        rows = self._db.execute(
            f"SELECT {_STAGE_COLUMNS} FROM draft_job_stages "  # nosec B608
            "WHERE job_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()
        return [_row_to_stage(r) for r in rows]

    def _mark_abandoned_stages_worker_restart_locked(self, job_id: int) -> int:
        """Mark a job's abandoned ``running`` stage attempts ``failed``.

        Must be called inside an already-open write transaction — used both by
        the standalone public method below and by compile-job orphan recovery
        so both writes commit atomically together.
        """
        rows = self._db.execute(
            "SELECT id FROM draft_job_stages WHERE job_id = ? AND status = 'running'",
            (job_id,),
        ).fetchall()
        for row in rows:
            _check_transition(
                "stage", "running", "failed",
                _STAGE_TRANSITIONS, _STAGE_RECOVERY_TRANSITIONS,
                allow_recovery=True,
            )
            self._db.execute(
                "UPDATE draft_job_stages SET status = 'failed', "
                "error_code = 'worker_restart', "
                "error_message = 'stage attempt abandoned by a worker restart', "
                "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(row[0]),),
            )
        return len(rows)

    def mark_abandoned_stages_worker_restart(self, *, job_id: int) -> int:
        """Settle every ``running`` stage attempt of a job onto ``failed``.

        Returns:
            The number of stage attempts marked abandoned.
        """
        self._begin_immediate()
        try:
            count = self._mark_abandoned_stages_worker_restart_locked(job_id)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return count

    # ── evidence ─────────────────────────────────────────────────────────

    def insert_evidence(
        self,
        *,
        job_id: int,
        label: str,
        source_kind: str,
        title: str,
        passage: str,
        source_content_sha256: str,
        draft_input_id: Optional[int] = None,
        file_id: Optional[int] = None,
        wiki_page_id: Optional[int] = None,
        wiki_claim_id: Optional[int] = None,
        kms_entry_id: Optional[int] = None,
        chunk_uid: Optional[str] = None,
        page_number: Optional[int] = None,
        section: Optional[str] = None,
        retrieval_score: Optional[float] = None,
        authority: str = "unknown",
        as_of_date: Optional[str] = None,
        source_updated_at: Optional[str] = None,
    ) -> int:
        """Insert one immutable retrieval-time evidence snapshot.

        ``passage_sha256`` is always computed here from ``passage`` — never
        accepted from the caller — so the stored hash can never drift from the
        stored text (SPEC 5.6: later vault mutation must not change historical
        evidence).

        Raises:
            DraftValidationError: ``source_kind`` is unknown, or more/fewer
                than one identity family is populated for it.
            DraftConflictError: ``(job_id, label)`` already exists.
        """
        if source_kind not in EVIDENCE_SOURCE_KINDS:
            raise DraftValidationError(
                f"unknown evidence source_kind: {source_kind!r}"
            )
        validate_evidence_identity(
            source_kind=source_kind,
            draft_input_id=draft_input_id,
            file_id=file_id,
            wiki_page_id=wiki_page_id,
            wiki_claim_id=wiki_claim_id,
            kms_entry_id=kms_entry_id,
        )
        passage_sha256 = sha256_text(passage)
        self._begin_immediate()
        try:
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_evidence (job_id, label, source_kind, "
                    "draft_input_id, file_id, wiki_page_id, wiki_claim_id, "
                    "kms_entry_id, chunk_uid, title, passage, passage_sha256, "
                    "source_content_sha256, page_number, section, retrieval_score, "
                    "authority, as_of_date, source_updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        label,
                        source_kind,
                        draft_input_id,
                        file_id,
                        wiki_page_id,
                        wiki_claim_id,
                        kms_entry_id,
                        chunk_uid,
                        title,
                        passage,
                        passage_sha256,
                        source_content_sha256,
                        page_number,
                        section,
                        retrieval_score,
                        authority,
                        as_of_date,
                        source_updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DraftConflictError(
                    f"evidence label {label!r} already recorded for job {job_id}"
                ) from exc
            evidence_id = int(cur.lastrowid)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return evidence_id

    def delete_evidence_for_job(self, *, job_id: int) -> int:
        """Remove this job's evidence rows so a re-run can replay cleanly.

        Scoped strictly to ``job_id``. Evidence is immutable for a COMPLETED
        historical job; this exists only for the case where a compile crashed
        partway through snapshotting and the same job is being resumed, where
        the alternative is a permanent UNIQUE(job_id, label) conflict.

        Returns:
            The number of rows removed.
        """
        self._begin_immediate()
        try:
            cur = self._db.execute(
                "DELETE FROM draft_evidence WHERE job_id = ?", (job_id,)
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return int(cur.rowcount or 0)

    def list_evidence(
        self,
        *,
        job_id: Optional[int] = None,
        revision_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DraftEvidenceRecord]:
        """Page through evidence for a compile job, oldest first.

        Exactly one of ``job_id``/``revision_id`` must scope the query — an
        unscoped listing would cross draft boundaries.

        Raises:
            DraftValidationError: Neither ``job_id`` nor ``revision_id`` given.
        """
        if job_id is None and revision_id is None:
            raise DraftValidationError(
                "list_evidence requires job_id or revision_id"
            )
        resolved_job_id = job_id
        if revision_id is not None:
            row = self._db.execute(
                "SELECT job_id FROM draft_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            if row is None or row[0] is None:
                return []
            if job_id is not None and int(row[0]) != job_id:
                return []
            resolved_job_id = int(row[0])
        rows = self._db.execute(
            f"SELECT {_EVIDENCE_COLUMNS} FROM draft_evidence "  # nosec B608
            "WHERE job_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
            (resolved_job_id, limit, offset),
        ).fetchall()
        return [_row_to_evidence(r) for r in rows]

    # ── evidence freshness (SPEC section 12.6) ───────────────────────────

    def list_evidence_identities(
        self, *, job_id: int, limit: int = 200, offset: int = 0
    ) -> list[DraftEvidenceIdentity]:
        """Page through one job's evidence *identities* (no passages).

        The freshness re-resolution in
        :mod:`app.services.draft_evidence_freshness` only needs identity,
        hashes, and update/delete metadata. Selecting the (potentially large)
        ``passage`` column for every row would make a bounded re-resolution
        pass unboundedly expensive, so it is deliberately excluded here.
        """
        rows = self._db.execute(
            f"SELECT {_EVIDENCE_IDENTITY_COLUMNS} "  # nosec B608
            "FROM draft_evidence e JOIN draft_jobs j ON j.id = e.job_id "
            "WHERE e.job_id = ? ORDER BY e.id ASC LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()
        return [_row_to_evidence_identity(r) for r in rows]

    def list_evidence_identities_for_source(
        self, *, source_kind: str, source_id: int, limit: int = 200, offset: int = 0
    ) -> list[DraftEvidenceIdentity]:
        """Find every evidence row pointing at one external source.

        ``source_kind`` is one of :data:`EVIDENCE_SOURCE_FILTERS` — note that
        Wiki has *two* identities (``wiki_page`` and ``wiki_claim``) because a
        page change invalidates every row with that ``wiki_page_id`` while a
        claim change invalidates only rows carrying that ``wiki_claim_id``
        (SPEC 12.6). Each filter is written to hit one of the partial identity
        indexes created in ``_DRAFT_ROOM_PIPELINE_DDL``.

        Raises:
            DraftValidationError: ``source_kind`` is not a known identity.
        """
        predicate = EVIDENCE_SOURCE_FILTERS.get(source_kind)
        if predicate is None:
            raise DraftValidationError(
                f"unknown evidence source identity: {source_kind!r}"
            )
        rows = self._db.execute(
            f"SELECT {_EVIDENCE_IDENTITY_COLUMNS} "  # nosec B608
            "FROM draft_evidence e JOIN draft_jobs j ON j.id = e.job_id "
            f"WHERE {predicate} ORDER BY e.id ASC LIMIT ? OFFSET ?",
            (source_id, limit, offset),
        ).fetchall()
        return [_row_to_evidence_identity(r) for r in rows]

    def mark_evidence_source_deleted(
        self, *, evidence_ids: Iterable[int], commit: bool = True
    ) -> int:
        """Stamp ``source_deleted_at`` on evidence whose source disappeared.

        The row itself is *never* deleted: SPEC 5.6 requires the historical
        passage to survive until the whole draft is deleted, and SPEC 12.6
        requires it to be clearly marked so it is never reused. Already-marked
        rows keep their original timestamp, which makes repeated hook/reconciler
        passes idempotent.
        """
        ids = [int(v) for v in evidence_ids]
        if not ids:
            return 0
        marked = 0
        for chunk in iter_chunks(ids, 500):
            placeholders = ",".join("?" for _ in chunk)
            cur = self._db.execute(
                "UPDATE draft_evidence SET source_deleted_at = CURRENT_TIMESTAMP "  # nosec B608
                f"WHERE id IN ({placeholders}) AND source_deleted_at IS NULL",
                tuple(chunk),
            )
            marked += cur.rowcount
        if commit:
            self._db.commit()
        return marked

    def apply_evidence_invalidation(
        self,
        *,
        draft_id: int,
        revision_id: int,
        job_id: Optional[int],
        reason: str,
        evidence_ids: Iterable[int],
        actor_user_id: Optional[int] = None,
    ) -> bool:
        """Record a SPEC 12.6 invalidation **inside the caller's transaction**.

        Deliberately does not begin or commit a transaction: it is called both
        from the Ready transaction (already holding ``BEGIN IMMEDIATE``) and
        from the mutation hooks/reconciler, and either way the invalidation
        must land atomically with whatever else the caller is doing.

        Performs the three mandated effects:

        1. the affected current revision becomes ``fact_status='invalidated'``;
        2. a **non-waivable** ``blocker`` finding (``evidence_changed`` or
           ``source_deleted``) is opened — one per rule per revision, so a
           repeated pass does not pile up duplicates;
        3. a ``ready`` draft moves back to ``needs_review`` through the normal
           transition table.

        Returns:
            True when anything changed (first invalidation), False when the
            revision was already invalidated with this rule's blocker open.

        Raises:
            DraftValidationError: ``reason`` is not a known invalidation reason.
        """
        if reason not in EVIDENCE_INVALIDATION_REASONS:
            raise DraftValidationError(f"unknown invalidation reason: {reason!r}")
        ids = sorted({int(v) for v in evidence_ids})
        changed = False

        cur = self._db.execute(
            "UPDATE draft_revisions SET fact_status = 'invalidated' "
            "WHERE id = ? AND draft_id = ? AND fact_status != 'invalidated'",
            (revision_id, draft_id),
        )
        changed = changed or cur.rowcount > 0

        existing = self._db.execute(
            "SELECT id FROM draft_findings WHERE draft_id = ? AND revision_id = ? "
            "AND rule_id = ? AND status = 'open' LIMIT 1",
            (draft_id, revision_id, reason),
        ).fetchone()
        if existing is None:
            self._db.execute(
                "INSERT INTO draft_findings (draft_id, revision_id, job_id, stage, "
                "rule_id, rule_version, category, severity, waivable, message) "
                "VALUES (?, ?, ?, 'fact', ?, ?, 'factuality', 'blocker', 0, ?)",
                (
                    draft_id,
                    revision_id,
                    job_id,
                    reason,
                    EVIDENCE_INVALIDATION_RULE_VERSION,
                    _EVIDENCE_INVALIDATION_MESSAGES[reason],
                ),
            )
            changed = True

        draft_row = self._db.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if draft_row is not None and str(draft_row[0]) == "ready":
            _check_transition(
                "draft",
                "ready",
                "needs_review",
                _DRAFT_TRANSITIONS,
                _DRAFT_RECOVERY_TRANSITIONS,
            )
            self._db.execute(
                "UPDATE drafts SET status = 'needs_review', ready_revision_id = NULL, "
                "ready_by = NULL, ready_at = NULL, lock_version = lock_version + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'ready'",
                (draft_id,),
            )
            changed = True

        if changed:
            # IDs and reason codes only — never passages, titles, or paths.
            self._insert_event(
                draft_id=draft_id,
                event_type="evidence_invalidated",
                actor_user_id=actor_user_id,
                job_id=job_id,
                revision_id=revision_id,
                payload={"reason": reason, "evidence_count": len(ids)},
            )
        return changed

    def list_ready_drafts_page(
        self, *, after_id: int = 0, limit: int = 50
    ) -> list[tuple[int, int, int, int, Optional[int]]]:
        """Keyset page of Ready drafts joined to their current revision.

        Used by the startup reconciler. Keyset (``id > after_id``) rather than
        OFFSET so a page cannot be skipped when an earlier draft leaves Ready
        mid-pass.

        Returns:
            ``(draft_id, owner_id, vault_id, revision_id, revision_job_id)``
            tuples ordered by draft id.
        """
        rows = self._db.execute(
            "SELECT d.id, d.created_by, d.vault_id, r.id, r.job_id "
            "FROM drafts d JOIN draft_revisions r "
            "ON r.draft_id = d.id AND r.is_current = 1 "
            "WHERE d.status = 'ready' AND d.id > ? ORDER BY d.id ASC LIMIT ?",
            (int(after_id), int(limit)),
        ).fetchall()
        return [
            (
                int(r[0]),
                int(r[1]),
                int(r[2]),
                int(r[3]),
                None if r[4] is None else int(r[4]),
            )
            for r in rows
        ]

    # ── claims ───────────────────────────────────────────────────────────

    def insert_claim(
        self,
        *,
        revision_id: int,
        ordinal: int,
        claim_text: str,
        span_start: int,
        span_end: int,
        claim_type: str,
        status: str,
        severity: str,
        rationale: str = "",
        retrieval_audit_json: str = "{}",
    ) -> int:
        """Insert one immutable claim, hashing its exact revision span.

        Raises:
            DraftValidationError: An enum value is unknown, or the span is out
                of bounds for the revision's stored content.
            DraftNotFoundError: The revision does not exist.
            DraftConflictError: ``(revision_id, ordinal)`` already exists.
        """
        if claim_type not in CLAIM_TYPES:
            raise DraftValidationError(f"unknown claim_type: {claim_type!r}")
        if status not in CLAIM_STATUSES:
            raise DraftValidationError(f"unknown claim status: {status!r}")
        if severity not in CLAIM_SEVERITIES:
            raise DraftValidationError(f"unknown claim severity: {severity!r}")
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT content_md FROM draft_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("revision not found")
            claim_sha256 = validate_claim_span(row[0], span_start, span_end)
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_claims (revision_id, ordinal, claim_text, "
                    "claim_sha256, span_start, span_end, claim_type, status, "
                    "severity, rationale, retrieval_audit_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        ordinal,
                        claim_text,
                        claim_sha256,
                        span_start,
                        span_end,
                        claim_type,
                        status,
                        severity,
                        rationale,
                        retrieval_audit_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DraftConflictError(
                    f"claim ordinal {ordinal} already recorded for revision "
                    f"{revision_id}"
                ) from exc
            claim_id = int(cur.lastrowid)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return claim_id

    def list_claims(
        self, *, revision_id: int, limit: int = 100, offset: int = 0
    ) -> list[DraftClaimRecord]:
        """Page through a revision's claims, in ordinal order."""
        rows = self._db.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM draft_claims "  # nosec B608
            "WHERE revision_id = ? ORDER BY ordinal ASC LIMIT ? OFFSET ?",
            (revision_id, limit, offset),
        ).fetchall()
        return [_row_to_claim(r) for r in rows]

    def set_claim_resolution(
        self,
        *,
        claim_id: int,
        target: str,
        resolved_by: Optional[int] = None,
        resolution_note: Optional[str] = None,
    ) -> None:
        """Move a claim's audit-only ``resolution`` field through its state machine.

        ``resolution`` never overrides SPEC 12.5 Ready eligibility on its own —
        it is an audit annotation, not a gate; callers computing Ready
        eligibility must still read ``status``/``severity`` directly.

        Raises:
            DraftValidationError: ``target`` is not a known resolution.
            DraftNotFoundError: The claim does not exist.
            InvalidTransitionError: The transition is not allowed.
        """
        if target not in CLAIM_RESOLUTIONS:
            raise DraftValidationError(f"unknown claim resolution: {target!r}")
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT resolution FROM draft_claims WHERE id = ?", (claim_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("claim not found")
            _check_transition(
                "claim", row[0], target,
                _CLAIM_RESOLUTION_TRANSITIONS,
                _CLAIM_RESOLUTION_RECOVERY_TRANSITIONS,
            )
            self._db.execute(
                "UPDATE draft_claims SET resolution = ?, resolved_by = ?, "
                "resolved_at = CURRENT_TIMESTAMP, resolution_note = ? WHERE id = ?",
                (target, resolved_by, resolution_note, claim_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    # ── claim sources ────────────────────────────────────────────────────

    def link_claim_source(
        self,
        *,
        claim_id: int,
        evidence_id: int,
        relationship: str,
        exact_quote: str,
        lexical_overlap_score: Optional[float] = None,
        passage_start: Optional[int] = None,
        passage_end: Optional[int] = None,
    ) -> int:
        """Attach one evidence citation to a claim.

        ``lexical_overlap_score`` is stored purely as a diagnostic and is never
        read as, or substituted for, the ``relationship`` verdict — a claim's
        support/contradiction status is a separate semantic determination.

        Raises:
            DraftValidationError: ``relationship``/``lexical_overlap_score`` is
                invalid, ``exact_quote`` does not match the evidence passage,
                or the evidence does not belong to the compile job that
                produced the claim's revision.
            DraftNotFoundError: The claim or evidence row does not exist.
            DraftConflictError: ``(claim_id, evidence_id, relationship)``
                already exists.
        """
        if relationship not in CLAIM_SOURCE_RELATIONSHIPS:
            raise DraftValidationError(f"unknown relationship: {relationship!r}")
        if lexical_overlap_score is not None and not (
            0.0 <= lexical_overlap_score <= 1.0
        ):
            raise DraftValidationError("lexical_overlap_score must be within [0, 1]")
        self._begin_immediate()
        try:
            claim_row = self._db.execute(
                "SELECT revision_id FROM draft_claims WHERE id = ?", (claim_id,)
            ).fetchone()
            if claim_row is None:
                raise DraftNotFoundError("claim not found")
            evidence_row = self._db.execute(
                "SELECT job_id, passage FROM draft_evidence WHERE id = ?",
                (evidence_id,),
            ).fetchone()
            if evidence_row is None:
                raise DraftNotFoundError("evidence not found")
            revision_row = self._db.execute(
                "SELECT job_id FROM draft_revisions WHERE id = ?", (claim_row[0],)
            ).fetchone()
            if revision_row is None or revision_row[0] is None:
                raise DraftValidationError(
                    "claim's revision has no compile job to validate evidence "
                    "ownership against"
                )
            if int(revision_row[0]) != int(evidence_row[0]):
                raise DraftValidationError(
                    "evidence does not belong to the compile job that produced "
                    "this claim's revision"
                )
            validate_exact_quote(evidence_row[1], exact_quote)
            try:
                cur = self._db.execute(
                    "INSERT INTO draft_claim_sources (claim_id, evidence_id, "
                    "relationship, exact_quote, passage_start, passage_end, "
                    "lexical_overlap_score) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        claim_id,
                        evidence_id,
                        relationship,
                        exact_quote,
                        passage_start,
                        passage_end,
                        lexical_overlap_score,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DraftConflictError(
                    "this claim/evidence/relationship combination is already "
                    "recorded"
                ) from exc
            claim_source_id = int(cur.lastrowid)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return claim_source_id

    def list_claim_sources(
        self, *, claim_id: int, limit: int = 100, offset: int = 0
    ) -> list[DraftClaimSourceRecord]:
        """Page through one claim's evidence citations."""
        rows = self._db.execute(
            f"SELECT {_CLAIM_SOURCE_COLUMNS} FROM draft_claim_sources "  # nosec B608
            "WHERE claim_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
            (claim_id, limit, offset),
        ).fetchall()
        return [_row_to_claim_source(r) for r in rows]

    # ── findings ─────────────────────────────────────────────────────────

    def insert_finding(
        self,
        *,
        draft_id: int,
        stage: str,
        rule_id: str,
        rule_version: str,
        category: str,
        severity: str,
        message: str,
        revision_id: Optional[int] = None,
        job_id: Optional[int] = None,
        waivable: bool = True,
        original_text: Optional[str] = None,
        suggestion: Optional[str] = None,
        span_start: Optional[int] = None,
        span_end: Optional[int] = None,
    ) -> int:
        """Insert one finding against a draft.

        When ``revision_id`` and both span bounds are given, the span is
        validated against that revision's exact content and its hash stored as
        ``span_text_sha256``.

        Raises:
            DraftValidationError: An enum value is unknown, or the span is out
                of bounds for the referenced revision.
            DraftNotFoundError: ``draft_id`` (or ``revision_id``) is absent.
        """
        if category not in FINDING_CATEGORIES:
            raise DraftValidationError(f"unknown finding category: {category!r}")
        if severity not in FINDING_SEVERITIES:
            raise DraftValidationError(f"unknown finding severity: {severity!r}")
        self._begin_immediate()
        try:
            draft_row = self._db.execute(
                "SELECT id FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if draft_row is None:
                raise DraftNotFoundError("draft not found")
            span_text_sha256 = None
            if revision_id is not None:
                rev_row = self._db.execute(
                    "SELECT content_md FROM draft_revisions "
                    "WHERE id = ? AND draft_id = ?",
                    (revision_id, draft_id),
                ).fetchone()
                if rev_row is None:
                    raise DraftNotFoundError("revision not found for this draft")
                if span_start is not None and span_end is not None:
                    span_text_sha256 = validate_claim_span(
                        rev_row[0], span_start, span_end
                    )
            cur = self._db.execute(
                "INSERT INTO draft_findings (draft_id, revision_id, job_id, stage, "
                "rule_id, rule_version, category, severity, waivable, message, "
                "original_text, suggestion, span_start, span_end, "
                "span_text_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft_id,
                    revision_id,
                    job_id,
                    stage,
                    rule_id,
                    rule_version,
                    category,
                    severity,
                    1 if waivable else 0,
                    message,
                    original_text,
                    suggestion,
                    span_start,
                    span_end,
                    span_text_sha256,
                ),
            )
            finding_id = int(cur.lastrowid)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return finding_id

    def list_findings(
        self,
        *,
        draft_id: int,
        owner_id: int,
        revision_id: Optional[int] = None,
        job_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DraftFindingRecord]:
        """Page through a draft's findings, newest first."""
        self.get_draft(draft_id, owner_id)
        where = ["draft_id = ?"]
        params: list[Any] = [draft_id]
        if revision_id is not None:
            where.append("revision_id = ?")
            params.append(revision_id)
        if job_id is not None:
            where.append("job_id = ?")
            params.append(job_id)
        clause = " AND ".join(where)
        # clause is built only from literal fragments; all values are bound parameters
        rows = self._db.execute(
            f"SELECT {_FINDING_COLUMNS} FROM draft_findings WHERE {clause} "  # nosec B608
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [_row_to_finding(r) for r in rows]

    def apply_finding(
        self,
        *,
        finding_id: int,
        resolved_by: int,
        resolution_note: Optional[str] = None,
    ) -> None:
        """Mark a finding ``applied`` (its suggested fix was incorporated)."""
        self._set_finding_status(
            finding_id=finding_id,
            target="applied",
            resolved_by=resolved_by,
            resolution_note=resolution_note,
        )

    def dismiss_finding(
        self,
        *,
        finding_id: int,
        resolved_by: int,
        resolution_note: Optional[str] = None,
    ) -> None:
        """Mark a finding ``dismissed``.

        Raises:
            DraftNotFoundError: The finding does not exist.
            DraftValidationError: The finding is a ``blocker`` — blockers
                cannot be dismissed, only applied or waived.
        """
        row = self._db.execute(
            "SELECT severity FROM draft_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if row is None:
            raise DraftNotFoundError("finding not found")
        if row[0] == "blocker":
            raise DraftValidationError("blocker findings cannot be dismissed")
        self._set_finding_status(
            finding_id=finding_id,
            target="dismissed",
            resolved_by=resolved_by,
            resolution_note=resolution_note,
        )

    def waive_finding(
        self,
        *,
        finding_id: int,
        resolved_by: int,
        resolution_note: str,
        waiver_rule_version: str,
        waiver_text_sha256: Optional[str] = None,
    ) -> None:
        """Mark a finding ``waived``.

        Raises:
            DraftNotFoundError: The finding does not exist.
            DraftValidationError: The finding is not ``waivable``, or
                ``resolution_note`` is empty.
        """
        if not resolution_note or not resolution_note.strip():
            raise DraftValidationError(
                "waiving a finding requires a non-empty reason"
            )
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT status, waivable FROM draft_findings WHERE id = ?",
                (finding_id,),
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("finding not found")
            if not row[1]:
                raise DraftValidationError("this finding is not waivable")
            _check_transition(
                "finding", row[0], "waived",
                _FINDING_TRANSITIONS, _FINDING_RECOVERY_TRANSITIONS,
            )
            self._db.execute(
                "UPDATE draft_findings SET status = 'waived', resolved_by = ?, "
                "resolved_at = CURRENT_TIMESTAMP, resolution_note = ?, "
                "waiver_rule_version = ?, waiver_text_sha256 = ? WHERE id = ?",
                (
                    resolved_by,
                    resolution_note,
                    waiver_rule_version,
                    waiver_text_sha256,
                    finding_id,
                ),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def _set_finding_status(
        self,
        *,
        finding_id: int,
        target: str,
        resolved_by: Optional[int],
        resolution_note: Optional[str],
    ) -> None:
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT status FROM draft_findings WHERE id = ?", (finding_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFoundError("finding not found")
            _check_transition(
                "finding", row[0], target,
                _FINDING_TRANSITIONS, _FINDING_RECOVERY_TRANSITIONS,
            )
            self._db.execute(
                "UPDATE draft_findings SET status = ?, resolved_by = ?, "
                "resolved_at = CURRENT_TIMESTAMP, resolution_note = ? WHERE id = ?",
                (target, resolved_by, resolution_note, finding_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    # ── compile jobs ─────────────────────────────────────────────────────

    def claim_next_compile_job(self) -> Optional[DraftJobRecord]:
        """Atomically claim one pending ``compile`` job.

        Mirrors :meth:`claim_next_parse_job`: takes the write lock with
        ``BEGIN IMMEDIATE`` and only flips ``pending -> running`` while the
        status is *still* pending, so a second concurrent worker racing for
        the same row claims nothing rather than double-running it.
        """
        self._begin_immediate()
        try:
            row = self._db.execute(
                "SELECT id FROM draft_jobs WHERE status = 'pending' "
                "AND job_type = 'compile' ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                self._db.rollback()
                return None
            job_id = int(row[0])
            cur = self._db.execute(
                "UPDATE draft_jobs SET status = 'running', "
                "started_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'pending'",
                (job_id,),
            )
            if cur.rowcount == 0:
                self._db.rollback()
                return None
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        row = self._db.execute(
            f"SELECT {_JOB_COLUMNS} FROM draft_jobs WHERE id = ?",  # nosec B608
            (job_id,),
        ).fetchone()
        return None if row is None else _row_to_job(row)

    def get_job_json_field(self, *, job_id: int, field: str) -> dict:
        """Fetch one large compile JSON body for ``job_id``, decoded.

        The compile JSON bodies are deliberately excluded from
        ``_JOB_COLUMNS``/:class:`DraftJobRecord` because SPEC §8.2 requires
        large bodies to be fetched explicitly rather than embedded in every
        job summary. This is the supported accessor for them, so callers never
        need to hand-write SQL against ``draft_jobs``.

        Args:
            job_id: The compile job id.
            field: One of ``input_json``, ``result_json``,
                ``brief_snapshot_json``, ``model_snapshot_json``.

        Returns:
            The decoded object, or ``{}`` when the row or value is absent.

        Raises:
            ValueError: if ``field`` is not one of the four allowed names.
                The name is validated against a fixed allowlist because it is
                interpolated into the SQL statement.
        """
        if field not in _JOB_JSON_FIELDS:
            raise ValueError(
                f"field must be one of {sorted(_JOB_JSON_FIELDS)}, got {field!r}"
            )
        row = self._db.execute(
            f"SELECT {field} FROM draft_jobs WHERE id = ?",  # nosec B608
            (job_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return {}
        try:
            decoded = json.loads(row[0])
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def set_job_json_field(self, *, job_id: int, field: str, value: dict) -> None:
        """Persist one large compile JSON body for ``job_id``.

        Args:
            job_id: The compile job id.
            field: One of the four names accepted by :meth:`get_job_json_field`.
            value: A JSON-serialisable mapping.

        Raises:
            ValueError: if ``field`` is not allowlisted.
        """
        if field not in _JOB_JSON_FIELDS:
            raise ValueError(
                f"field must be one of {sorted(_JOB_JSON_FIELDS)}, got {field!r}"
            )
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
        self._begin_immediate()
        try:
            self._db.execute(
                f"UPDATE draft_jobs SET {field} = ? WHERE id = ?",  # nosec B608
                (payload, job_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def _settle_draft_after_lost_compile_locked(
        self, draft_id: int, target: str
    ) -> None:
        """Move a draft off ``queued``/``running`` when its compile is gone.

        Caller must already hold the transaction. Only acts when the draft is
        actually parked on a compile-lifecycle status, so a draft that has
        already moved on is left untouched.
        """
        row = self._db.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if row is None or row[0] not in ("queued", "running"):
            return
        _check_transition(
            "draft", row[0], target, _DRAFT_TRANSITIONS, _DRAFT_RECOVERY_TRANSITIONS
        )
        self._db.execute(
            "UPDATE drafts SET status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (target, draft_id),
        )

    def recover_orphaned_compile_jobs(self) -> int:
        """Return crashed ``running`` compile jobs to ``pending`` at startup.

        Also settles that job's own abandoned ``running`` stage attempts onto
        ``failed`` (``worker_restart``) in the same transaction: a completed
        stage row is immutable, so a resumed run must redo any stage that
        never finished rather than trust a half-written one.

        Returns:
            The number of jobs reset to pending (cancelled jobs are not counted).
        """
        self._begin_immediate()
        try:
            rows = self._db.execute(
                "SELECT id, draft_id, cancel_requested_at FROM draft_jobs "
                "WHERE status = 'running' AND job_type = 'compile'"
            ).fetchall()
            reset = 0
            for row in rows:
                job_id, draft_id = int(row[0]), int(row[1])
                cancel_requested_at = row[2]
                self._mark_abandoned_stages_worker_restart_locked(job_id)
                if cancel_requested_at is not None:
                    self._db.execute(
                        "UPDATE draft_jobs SET status = 'cancelled', "
                        "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (job_id,),
                    )
                    # The worker died before it could observe the cancellation,
                    # so the pipeline never settled the draft. Without this the
                    # draft stays on 'queued', whose only exits are
                    # running/failed/cancelled -- none of which can still
                    # happen -- leaving it permanently uncompilable and
                    # unarchivable. Same dead end as the pending-cancel case.
                    self._settle_draft_after_lost_compile_locked(
                        draft_id, "cancelled"
                    )
                    continue
                self._db.execute(
                    "UPDATE draft_jobs SET status = 'pending', started_at = NULL, "
                    "error_code = 'worker_restart', error_message = "
                    "'previous attempt abandoned by a worker restart' WHERE id = ?",
                    (job_id,),
                )
                reset += 1
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        if reset:
            logger.warning(
                "draft room: reset %d orphaned compile job(s) to pending after "
                "restart",
                reset,
            )
        return reset


def build_input_relpath(
    *, owner_id: int, draft_id: int, input_id: int, stored_name: str
) -> str:
    """Build the canonical relative storage path for an input.

    Layout (SPEC section 6.1), relative to ``data/draft-room``::

        <user_id>/<draft_id>/inputs/<input_id>/<uuid><extension>

    ``stored_name`` is a server-generated UUID filename — never the client's
    filename, which is retained only as display metadata.
    """
    return f"{owner_id}/{draft_id}/inputs/{input_id}/{stored_name}"


def canonical_json(payload: Any) -> str:
    """Serialize with sorted keys and fixed separators for stable hashing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def json_or_default(raw: Optional[str], default: Any) -> Any:
    """Parse stored JSON defensively, falling back to ``default`` when unusable."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def iter_chunks(values: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """Yield ``values`` in lists of at most ``size`` items."""
    chunk: list[Any] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
