"""Deterministic compile orchestrator for the Draft Room editorial pipeline.

This module is the keystone of Draft Room PR 3 (issue #436,
``specs/draft-room/SPEC.md`` §10 and §11). :func:`run_compile` is the single
entry point ``DraftJobProcessor`` calls for a claimed ``compile`` job; every
other name here is scaffolding for it.

The law this module exists to enforce
-------------------------------------

1. **Python decides the order, never a model.** :data:`COMPILE_STAGE_ORDER` is
   a literal tuple and :meth:`_CompileRun.execute` walks it index by index. No
   model output is ever consulted to pick the next stage or to skip a gate.
2. **Copy runs before Standards**, always — they are adjacent entries in that
   tuple and the correction loop re-runs them in the same order.
3. **Any semantic edit invalidates factual approval.** Fact is the last thing
   that touches the candidate before Assemble, so a Copy/Standards edit is
   *structurally* upstream of a fresh Fact run. The correction loop is bounded
   by ``settings.draft_qa_retry_limit``; reaching that cap ends the loop and
   leaves residual issues as visible findings (SPEC §11.8), rather than
   discarding an otherwise complete Fact-verified draft.
4. **Fact never mutates prose.** It receives an immutable ``str`` candidate and
   returns a report; :meth:`_CompileRun._stage_fact` re-hashes the candidate
   afterwards and hard-fails on any difference.
5. **Assemble may not alter a byte.** Its input hash must equal the successful
   Fact candidate hash or it fails with :data:`CODE_ASSEMBLE_HASH_MISMATCH`.
   There is no fixup branch.
6. **Assemble finishes in ``needs_review``.** The literal string ``"ready"`` is
   never written by this module.
7. **SPEC §11.10 provisional assembly is not implemented here.** See
   :data:`PROVISIONAL_ASSEMBLY_ENABLED`.
8. **Persist and hash before advancing.** Every stage writes a
   ``draft_job_stages`` row with its canonical artifact JSON and SHA-256 before
   the orchestrator moves on.
9. **No SQLite connection is ever alive across an ``await``.** Every database
   step is a *synchronous* ``_db_*`` method that opens a pooled connection,
   writes, and releases it, driven through ``asyncio.to_thread``. No ``await``
   appears inside any of those methods, and no connection object is ever stored
   on ``self``.
10. **Budgets are hard.** Wall clock (``draft_job_timeout_seconds``), model
    calls (``draft_job_max_model_calls``) and sections (``draft_max_sections``)
    each have a stable, non-retryable failure code.
11. **Bounded automatic retry.** Only transient provider/retrieval faults are
    retried, at most ``settings.draft_transient_retry_limit`` times with
    bounded backoff. ``ProviderPolicyError``, validation, content-size and
    budget failures are never retried.
12. **Exactly one structured-output repair.** Then
    :data:`CODE_INVALID_STAGE_OUTPUT`.
13. ``assert_provider_allowed`` runs before *every* model call, not only at
    enqueue.
14. An observed cancellation discards an in-flight provider result.
15. Resume reuses an upstream checkpoint only on a total match of compile
    fingerprint, prompt bundle, source snapshot and the stage's own input hash.
16. Fact decomposes the candidate into atomic claims with the six SPEC §12.3
    statuses, pins every sourced classification to a snapshotted passage, runs
    claim-specific retrieval when Research evidence is insufficient, and
    records the retrieval audit *even when nothing is found*.
17. Forbidden vocabulary (``confidence``, ``support``, ``correctness``,
    ``entailment``, ``verification``, ``support_probability``) appears nowhere.
    The only score name is ``lexical_overlap_score``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Sequence

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.services.citation_validator import calculate_citation_lexical_overlap
from app.services.draft_prompts import (
    PROMPT_BUNDLE_VERSION,
    PROMPTS,
    CopyEdit,
    CopyReport,
    DraftArtifact,
    DraftSection,
    FactClaim,
    FactReport,
    IntakeInputRecord,
    IntakeManifest,
    LintReport,
    ModelCallAudit,
    OutlineArtifact,
    PromptDefinition,
    ResearchPacket,
    RetrievalAudit,
    StandardsReport,
)
from app.services.draft_provider_policy import (
    ProviderPolicyError,
    assert_provider_allowed,
    draft_http_client_kwargs,
    provider_snapshot,
)
from app.services.draft_quality import (
    BOILERPLATE_RULE_VERSION,
    apply_bounded_rewrites,
    run_deterministic_lint,
)
from app.services.draft_store import (
    DraftConflictError,
    DraftStore,
    DraftValidationError,
    canonical_json,
    sha256_text,
    validate_exact_quote,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.database import SQLiteConnectionPool
    from app.services.draft_store import DraftJobRecord, DraftStageRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical stage order (SPEC §10.2, §11)
# ---------------------------------------------------------------------------

COMPILE_STAGE_ORDER: tuple[str, ...] = (
    "intake",
    "research",
    "outline",
    "draft",
    "lint",
    "copy",
    "standards",
    "fact",
    "assemble",
)

#: SPEC §11.10 shipped a temporary "store the linted candidate without Copy,
#: Standards or Fact" path for PR 2. PR 3 (this module) must disable it for all
#: new jobs. It is disabled by *absence*: this orchestrator contains no branch
#: that can create a revision without a completed Fact stage — Assemble hard
#: -fails with :data:`CODE_ASSEMBLE_WITHOUT_FACT` when no Fact candidate hash is
#: present, and this flag exists so the guarantee is greppable and asserted at
#: runtime rather than merely documented. Historical provisional revisions
#: written by PR 2 remain readable; nothing here can produce a new one.
PROVISIONAL_ASSEMBLY_ENABLED: bool = False


# ---------------------------------------------------------------------------
# Stable failure codes. Nothing else may reach ``set_job_status``.
# ---------------------------------------------------------------------------

CODE_INVALID_COMPILE_JOB = "invalid_compile_job"
CODE_BRIEF_CONTRACT_FAILED = "brief_contract_failed"
CODE_INPUTS_NOT_READY = "inputs_not_ready"
CODE_MANUSCRIPT_REQUIRED = "manuscript_required"
CODE_INPUT_LIMIT_EXCEEDED = "input_limit_exceeded"
CODE_RESEARCH_BLOCKED = "research_blocked"
CODE_RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"
CODE_OUTLINE_REJECTED = "outline_rejected"
CODE_SECTION_BUDGET_EXCEEDED = "section_budget_exceeded"
CODE_MODEL_CALL_BUDGET_EXCEEDED = "model_call_budget_exceeded"
CODE_JOB_TIMEOUT = "job_timeout"
CODE_INVALID_STAGE_OUTPUT = "invalid_stage_output"
CODE_FACT_MUTATED_CANDIDATE = "fact_mutated_candidate"
CODE_ASSEMBLE_WITHOUT_FACT = "assemble_without_fact"
CODE_ASSEMBLE_HASH_MISMATCH = "assemble_candidate_hash_mismatch"
CODE_ASSEMBLE_VALIDATION_REQUIRES_MUTATION = "assemble_validation_requires_mutation"
CODE_PROVIDER_UNAVAILABLE = "provider_unavailable"
CODE_JOB_CANCELLED = "job_cancelled"
CODE_INTERNAL_ERROR = "internal_error"

#: Longest bounded backoff between automatic transient retries, in seconds.
_MAX_BACKOFF_SECONDS = 8.0

#: Continuity budget for section drafting (SPEC §11.4: "no more than the last
#: two paragraphs of the previous generated section").
_CONTINUITY_PARAGRAPHS = 2

#: Any citation label shape recognised by ``citation_validator``.
_CITATION_LABEL_RE = re.compile(r"\[(S|M|W|K|D)(\d+)\]")

#: Reasoning-trace markers Assemble refuses to ship (SPEC §11.9 step 1). Fact
#: candidates carrying these need a prose mutation, which Assemble may not make.
_REASONING_TRACE_MARKERS: tuple[str, ...] = (
    "<think>",
    "</think>",
    "<thinking>",
    "</thinking>",
    "<|channel|>analysis",
)

#: Claim statuses that must be pinned to a snapshotted passage (SPEC §12.3).
_SOURCED_CLAIM_STATUSES: frozenset[str] = frozenset(
    {"supported", "contradicted", "ambiguous", "stale"}
)

#: Claim statuses that are non-waivable Ready blockers (SPEC §12.5 rule 3).
_BLOCKING_CLAIM_STATUSES: frozenset[str] = frozenset(
    {"contradicted", "unsupported", "ambiguous", "stale"}
)

#: Fact verdicts that demand a targeted correction, i.e. re-enter the bounded
#: Copy -> Standards -> Fact loop (SPEC §11.8).
_CORRECTABLE_CLAIM_STATUSES: frozenset[str] = frozenset({"contradicted", "stale"})

#: Draft statuses Assemble may legally leave as ``needs_review`` (SPEC §10.3).
_ASSEMBLE_ALLOWED_PRIOR_DRAFT_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "needs_review"}
)

# High-stakes detectors (SPEC §12.4). Deterministic Python, applied on top of —
# never instead of — the model's own ``high_stakes`` flag, which is only ever
# widened, never cleared.
_NUMBER_RE = re.compile(r"\d")
_DATE_RE = re.compile(
    r"\b(19|20)\d{2}\b|\b(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b",
    re.IGNORECASE,
)
_OBLIGATION_RE = re.compile(
    r"\b(must|shall|required|requires|obligated|obligation|mandatory|prohibited|"
    r"liable|liability|entitled)\b",
    re.IGNORECASE,
)
_SAFETY_RE = re.compile(
    r"\b(safe|safety|unsafe|hazard|hazardous|toxic|danger|dangerous|fatal|"
    r"injury|harm|risk of)\b",
    re.IGNORECASE,
)
_CAUSAL_RE = re.compile(
    r"\b(because|causes|caused|causing|leads to|led to|results in|resulted in|"
    r"due to|therefore|consequently)\b",
    re.IGNORECASE,
)
_PROPER_NAME_RE = re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b")
_QUOTED_SPAN_RE = re.compile(r"\"([^\"\n]{4,})\"|“([^”\n]{4,})”")


# ---------------------------------------------------------------------------
# Failure type
# ---------------------------------------------------------------------------


class CompileFailure(Exception):
    """A compile failure carrying a stable machine code and a retry verdict.

    ``retryable`` is the *automatic* retry verdict consumed by
    :class:`~app.services.draft_job_processor.DraftJobProcessor`. It is True
    only for transient provider/retrieval faults. Authorization, validation,
    content-size, provider-policy and hard-budget failures are always False
    (SPEC §10.2).
    """

    def __init__(
        self, code: str, *, retryable: bool = False, message: Optional[str] = None
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable


class _CompileCancelled(Exception):
    """Internal signal: the job's cancellation was observed. Not a failure."""


# ---------------------------------------------------------------------------
# Injection seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineDeps:
    """Injection seam so tests use deterministic fakes for model + retrieval.

    Attributes:
        retrieve_sources: the BOUND ``RAGEngine.retrieve_sources`` method
            (SPEC §4.4). Called as
            ``retrieve_sources(query, vault_id, limit=..., source_kinds=...)``.
        complete: one logical-mode model call. Called as
            ``complete(prompt, logical_mode=..., temperature=..., sensitive=...)``
            and returns the raw response text.
        now: clock, injected so wall-clock budget tests are deterministic.
        publish: stage-level SSE notification, called as
            ``publish(event_type, draft_id=..., job_id=..., stage=..., ...)``
            AFTER the stage transaction commits. SPEC §8.4 lists
            ``stage_started``/``stage_completed`` as part of the SSE contract;
            SSE is notification only, so a publish failure must never affect
            the job. Defaults to a no-op so tests stay deterministic and the
            pipeline never hard-depends on a bus.
    """

    retrieve_sources: Callable[..., Awaitable[Any]]
    complete: Callable[..., Awaitable[str]]
    now: Callable[[], datetime]
    publish: Callable[..., None] = lambda *a, **k: None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _provider_base_url(logical_mode: str) -> str:
    """Resolve the provider base URL for a logical mode.

    SPEC §14.2 requires single-model deployments to work, so an unconfigured
    ``instant`` endpoint degrades to the ``thinking`` one rather than failing.
    """
    if logical_mode == "instant":
        return (settings.instant_chat_url or settings.ollama_chat_url or "").strip()
    return (settings.ollama_chat_url or "").strip()


def _assert_redirects_disabled(client: object) -> None:
    """Fail closed unless the provider client refuses HTTP redirects.

    SPEC §9.2 forbids replaying manuscript content or authorization headers to
    a redirect target. ``draft_http_client_kwargs()`` is the single source of
    truth for that setting; this check makes the discipline unforgettable at
    the call site instead of trusting the client's constructor.
    """
    expected = bool(draft_http_client_kwargs().get("follow_redirects", False))
    inner = getattr(client, "_client", None)
    if inner is None:
        return
    if bool(getattr(inner, "follow_redirects", True)) != expected:
        raise ProviderPolicyError(
            "Provider client does not have redirects disabled.",
            code="provider_redirect_blocked",
        )


async def _default_complete(
    prompt: str,
    *,
    logical_mode: str,
    temperature: float,
    sensitive: bool,
) -> str:
    """Production model call for one Draft Room stage.

    Uses the repository's ordinary logical-mode clients, which already pin
    ``follow_redirects=False`` and an SSRF-revalidating transport; the pin is
    re-asserted here against :func:`draft_http_client_kwargs`. Provider
    exceptions are never logged with their message — only their type name —
    because they can carry response bodies (SPEC §20).
    """
    from app.services.llm_client import create_instant_client, create_thinking_client

    client = (
        create_instant_client() if logical_mode == "instant" else create_thinking_client()
    )
    try:
        await client.start()
        _assert_redirects_disabled(client)
        # SPEC §9.2: record provider kind/model, never keys or endpoints.
        logger.debug("draft compile: provider %s", provider_snapshot(client))
        return await client.chat_completion(
            [{"role": "user", "content": prompt}],
            temperature=temperature,
        )
    finally:
        await client.close()


async def _unwired_retrieval(*_args: object, **_kwargs: object) -> Any:
    raise CompileFailure(
        CODE_RETRIEVAL_UNAVAILABLE,
        retryable=False,
        message="no RAGEngine was injected into PipelineDeps",
    )


def default_deps(*, engine: object | None = None) -> PipelineDeps:
    """Production dependencies for :func:`run_compile`.

    ``engine`` is the live ``RAGEngine`` singleton (``app.state.rag_engine``).
    It is an optional keyword so the pinned zero-argument call remains valid;
    when it is omitted, retrieval fails closed with
    :data:`CODE_RETRIEVAL_UNAVAILABLE` rather than silently returning an empty
    result (SPEC §20: never convert a retrieval error into an empty success).
    ``rag_engine`` is imported lazily because it drags in the vector store.
    """
    retrieve = getattr(engine, "retrieve_sources", None) if engine else None
    return PipelineDeps(
        retrieve_sources=retrieve or _unwired_retrieval,
        complete=_default_complete,
        now=_utcnow,
        publish=_default_publish,
    )


def _default_publish(event_type: str, **fields: Any) -> None:
    """Publish one stage event on the process-wide Draft Room bus (SPEC §8.4).

    ``build_event`` fails closed on its field allowlist, so an event carrying
    anything content-bearing raises here and is swallowed by
    ``_CompileRun._notify`` rather than reaching a subscriber.
    """
    from app.services.draft_events import build_event, get_draft_event_bus

    draft_id = int(fields["draft_id"])
    get_draft_event_bus().publish(draft_id, build_event(event_type, **fields))


# ---------------------------------------------------------------------------
# Immutable per-job context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InputSnapshot:
    """One project input as it stood when the compile job started."""

    input_id: int
    role: str
    authority: str
    as_of_date: Optional[str]
    raw_sha256: str
    parsed_sha256: str
    character_count: int
    parsed_text: str
    locked_spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class CompileContext:
    """Everything a compile run needs that never changes mid-run.

    Deliberately frozen: budgets that *do* change (the model-call counter) live
    on the mutable :class:`_Budget`, so nothing in the stage code can quietly
    relax a limit.
    """

    job_id: int
    draft_id: int
    owner_id: int
    vault_id: int
    tier: str
    mode: str
    brief_json: str
    brief_hash: str
    inputs: tuple[_InputSnapshot, ...]
    prompt_bundle_version: str
    compile_fingerprint: str
    start_stage: Optional[str]
    started_at: datetime
    deadline: datetime
    max_model_calls: int
    max_sections: int
    max_correction_loops: int
    transient_retry_limit: int
    retrieval_limit: int
    resume_allowed: bool

    @property
    def sensitive(self) -> bool:
        """Sensitive tier picks the stricter provider allowlist (SPEC §9.2)."""
        return self.tier == "sensitive"

    @property
    def manuscript_inputs(self) -> tuple[_InputSnapshot, ...]:
        return tuple(i for i in self.inputs if i.role == "manuscript")


@dataclass
class _Budget:
    """Mutable budget counters (SPEC §10.2: enforce all three)."""

    deadline: datetime
    max_model_calls: int
    model_calls: int = 0


@dataclass
class _PendingFinding:
    """A finding held until Assemble creates the revision it attaches to."""

    stage: str
    rule_id: str
    rule_version: str
    category: str
    severity: str
    message: str
    waivable: bool = True
    original_text: Optional[str] = None
    suggestion: Optional[str] = None
    span_start: Optional[int] = None
    span_end: Optional[int] = None


@dataclass
class _ClaimRow:
    """A resolved atomic claim ready to be written to ``draft_claims``."""

    ordinal: int
    claim_text: str
    span_start: int
    span_end: int
    claim_type: str
    status: str
    severity: str
    rationale: str
    retrieval_audit_json: str
    #: ``(evidence_label, relationship, exact_quote, lexical_overlap_score)``
    sources: tuple[tuple[str, str, str, Optional[float]], ...] = ()


@dataclass
class _TextResult:
    """Outcome of an editing desk: new text plus whether meaning moved."""

    text: str
    semantic_changed: bool
    applied_edits: tuple[CopyEdit, ...]


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _normalize_line_endings(text: str) -> str:
    """CRLF/CR -> LF, applied exactly once before the first candidate hash.

    SPEC §11: after this normalization the candidate is hashed as exact UTF-8
    bytes with no further Unicode, whitespace, or citation normalization.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _artifact_json(model: BaseModel) -> str:
    """UTF-8, sorted-key, compact-separator canonical JSON for an artifact."""
    return canonical_json(model.model_dump(mode="json"))


def _stage_input_hash(stage: str, ctx: CompileContext, payload: dict[str, Any]) -> str:
    """Hash the *exact* inputs of one stage.

    Resume correctness (SPEC §10.1 item 6) rides entirely on this: the hash
    folds in the compile fingerprint, the prompt bundle, the rendered prompt's
    own SHA-256 and every upstream artifact/candidate hash the stage consumes.
    A change anywhere upstream therefore changes this hash, and the stored
    checkpoint stops matching, so the stage re-runs.
    """
    prompt = PROMPTS.get(stage)
    body = {
        "stage": stage,
        "compile_fingerprint": ctx.compile_fingerprint,
        "prompt_bundle_version": ctx.prompt_bundle_version,
        "prompt_sha256": prompt.sha256 if prompt else "",
        "brief_hash": ctx.brief_hash,
        **payload,
    }
    return sha256_text(canonical_json(body))


def _extract_json_object(raw: str) -> Any:
    """Parse a model response that should be a single JSON object.

    Tolerates a leading/trailing code fence or prose, because prompt-instructed
    JSON is the only structured-output mechanism this repo can rely on
    (``response_format`` has no production caller). Anything else raises
    ``ValueError``, which the caller converts into the one permitted repair.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


def _is_transient(exc: BaseException) -> bool:
    """Classify a provider/retrieval fault as automatically retryable.

    Fails *closed*: an exception type this function does not recognise is not
    retried. ``ProviderPolicyError`` and Pydantic validation errors are
    explicitly non-transient (SPEC §10.2).
    """
    if isinstance(exc, (ProviderPolicyError, ValidationError, _CompileCancelled)):
        return False
    if isinstance(exc, CompileFailure):
        return exc.retryable
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, TimeoutError, OSError)):
        return True
    name = type(exc).__name__
    return name in {
        "LLMError",
        "CircuitBreakerError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "RequestError",
        "TransportError",
    }


def _backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff: 0.5s, 1s, 2s, ... capped."""
    return min(_MAX_BACKOFF_SECONDS, 0.5 * (2**max(attempt - 1, 0)))


def _paragraph_tail(text: str, paragraphs: int = _CONTINUITY_PARAGRAPHS) -> str:
    """Last ``paragraphs`` paragraphs of ``text`` — the only continuity allowed."""
    blocks = [p for p in text.split("\n\n") if p.strip()]
    return "\n\n".join(blocks[-paragraphs:]) if blocks else ""


def _is_high_stakes(claim: FactClaim) -> bool:
    """Deterministic SPEC §12.4 high-stakes classification.

    Names, numbers, dates, legal obligations, safety claims, causal claims and
    direct quotes are high-stakes. The model's own flag is only ever widened by
    this function, never cleared.
    """
    if claim.high_stakes or claim.claim_type == "quote":
        return True
    text = claim.proposition
    return bool(
        _NUMBER_RE.search(text)
        or _DATE_RE.search(text)
        or _OBLIGATION_RE.search(text)
        or _SAFETY_RE.search(text)
        or _CAUSAL_RE.search(text)
        or _PROPER_NAME_RE.search(text)
        or _QUOTED_SPAN_RE.search(text)
    )


def _quoted_spans(text: str) -> tuple[str, ...]:
    """Every straight/curly double-quoted run in ``text``."""
    out: list[str] = []
    for match in _QUOTED_SPAN_RE.finditer(text):
        out.append(match.group(1) or match.group(2) or "")
    return tuple(s for s in out if s)


def _apply_edits(text: str, edits: Sequence[CopyEdit]) -> _TextResult:
    """Apply precise, in-range desk edits highest-offset-first.

    An edit is applied only when the live text at ``[start:end)`` still hashes
    to the edit's ``before_sha256`` — a desk that describes a span it did not
    actually see is dropped rather than allowed to corrupt neighbouring prose
    (SPEC §11.6: never an unexplained wholesale rewrite).
    """
    applied: list[CopyEdit] = []
    new_text = text
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        if edit.start < 0 or edit.end > len(new_text) or edit.end < edit.start:
            continue
        current = new_text[edit.start : edit.end]
        if sha256_text(current) != edit.before_sha256:
            continue
        replacement = edit.after_excerpt
        if sha256_text(replacement) != edit.after_sha256:
            continue
        if replacement == current:
            continue
        new_text = new_text[: edit.start] + replacement + new_text[edit.end :]
        applied.append(edit)
    semantic = any(e.semantic_change for e in applied)
    return _TextResult(
        text=new_text, semantic_changed=semantic, applied_edits=tuple(applied)
    )


def _evidence_kwargs(snapshot: object) -> dict[str, Any]:
    """Map a ``draft_research`` evidence snapshot onto ``insert_evidence``.

    Read attribute-by-attribute rather than ``asdict``/``model_dump`` so the
    pipeline stays compatible with either a dataclass or a Pydantic model and
    never forwards a field ``insert_evidence`` does not accept.
    """
    required = ("label", "source_kind", "title", "passage", "source_content_sha256")
    optional = (
        "draft_input_id",
        "file_id",
        "wiki_page_id",
        "wiki_claim_id",
        "kms_entry_id",
        "chunk_uid",
        "page_number",
        "section",
        "retrieval_score",
        "as_of_date",
        "source_updated_at",
    )
    kwargs: dict[str, Any] = {name: getattr(snapshot, name) for name in required}
    for name in optional:
        kwargs[name] = getattr(snapshot, name, None)
    kwargs["authority"] = getattr(snapshot, "authority", None) or "unknown"
    return kwargs


def _single_source_policy(
    tier: str, *, sole_authority: bool
) -> tuple[str, bool, str]:
    """Severity/waivability for a single-source high-stakes claim (SPEC §12.5.4).

    Tiers may only tighten. ``standard`` keeps the visible ``single_source``
    warning SPEC §12.4 requires; ``high_stakes`` raises it to a waivable
    blocker so a human must attribute it explicitly; ``sensitive`` makes it
    non-waivable unless the sole source is the primary official authority for
    that exact proposition, which is the one carve-out the spec allows.

    An unknown tier is treated as the strictest, so a future tier cannot
    silently downgrade a known defect.
    """
    if tier == "standard":
        return "warning", True, "standard tier: visible warning"
    if tier == "high_stakes":
        return (
            "blocker",
            True,
            "high_stakes tier: waivable with explicit attribution and reason",
        )
    if sole_authority:
        return (
            "blocker",
            True,
            "sensitive tier: sole source is the primary authority for this claim",
        )
    return "blocker", False, "sensitive tier: corroboration required"


def _canonical_source_sha256_for(
    conn: "sqlite3.Connection",
    kwargs: dict[str, Any],
    *,
    draft_id: int,
    vault_id: int,
) -> Optional[str]:
    """Resolve the canonical whole-source hash for a pending evidence insert.

    ``draft_evidence_freshness.canonical_source_sha256`` takes a
    :class:`DraftEvidenceIdentity`, which normally comes from a persisted row.
    Here the row does not exist yet, so build the same projection from the
    insert kwargs. ``draft_id``/``vault_id`` MUST be the real values: the
    resolvers scope every lookup to the draft's vault, so a placeholder would
    resolve nothing and silently leave the passage hash in place. Only
    ``id``/``job_id`` are unused placeholders.

    Returns ``None`` when the source cannot be resolved, which the caller
    treats as "keep the snapshot value" rather than "changed" — deciding
    changed-versus-missing is
    :func:`draft_evidence_freshness.check_evidence_freshness`'s job.
    """
    from app.services.draft_evidence_freshness import canonical_source_sha256
    from app.services.draft_store import DraftEvidenceIdentity

    try:
        identity = DraftEvidenceIdentity(
            id=0,
            job_id=0,
            draft_id=draft_id,
            vault_id=vault_id,
            label=str(kwargs.get("label") or ""),
            source_kind=str(kwargs.get("source_kind") or ""),
            draft_input_id=kwargs.get("draft_input_id"),
            file_id=kwargs.get("file_id"),
            wiki_page_id=kwargs.get("wiki_page_id"),
            wiki_claim_id=kwargs.get("wiki_claim_id"),
            kms_entry_id=kwargs.get("kms_entry_id"),
            source_content_sha256=str(kwargs.get("source_content_sha256") or ""),
            source_updated_at=kwargs.get("source_updated_at"),
            source_deleted_at=None,
        )
        return canonical_source_sha256(conn, identity)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "draft compile: canonical source hash unresolved (%s)", type(exc).__name__
        )
        return None


def _load_research_runner() -> Callable[..., Awaitable[Any]]:
    """Resolve ``draft_research.run_research`` lazily.

    Imported at call time, not module import time, so this orchestrator loads
    (and lints) independently of its sibling module, and so tests can replace
    the resolver with a deterministic fake.
    """
    from app.services.draft_research import run_research

    return run_research


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class _StageOutcome:
    """What a stage produced, plus whether it was reused from a checkpoint."""

    artifact_json: str
    artifact_sha256: str
    reused: bool


class _CompileRun:
    """One compile job, start to finish.

    Connection discipline: every method whose name starts with ``_db_`` is
    synchronous, opens exactly one pooled connection, commits, and releases it
    before returning. None of them contain an ``await``, and no connection
    object is ever assigned to ``self`` — so no connection can be alive across
    an ``await`` in the async stage code, which reaches the database only
    through ``asyncio.to_thread(self._db_*)`` (SPEC §10.1 item 3).
    """

    def __init__(
        self, *, pool: "SQLiteConnectionPool", deps: PipelineDeps, ctx: CompileContext
    ) -> None:
        self._pool = pool
        self._deps = deps
        self._ctx = ctx
        self._budget = _Budget(
            deadline=ctx.deadline, max_model_calls=ctx.max_model_calls
        )
        self._attempts: dict[str, int] = {}
        self._checkpoints: dict[str, "DraftStageRecord"] = {}
        # Cross-stage state, all plain Python values.
        self._manifest: Optional[IntakeManifest] = None
        self._packet: Optional[ResearchPacket] = None
        self._evidence_ids: dict[str, int] = {}
        self._evidence_passages: dict[str, str] = {}
        #: label -> snapshotted authority, for the SPEC §12.5.4 sensitive-tier
        #: sole-source carve-out.
        self._evidence_authorities: dict[str, str] = {}
        self._outline: Optional[OutlineArtifact] = None
        self._draft_artifact: Optional[DraftArtifact] = None
        self._candidate: str = ""
        self._fact_report: Optional[FactReport] = None
        self._fact_candidate_sha256: Optional[str] = None
        self._claims: list[_ClaimRow] = []
        self._findings: list[_PendingFinding] = []
        self._correction_loops: int = 0
        self._source_snapshot_sha256: str = ""

    # -- entry ------------------------------------------------------------

    async def execute(self) -> None:
        """Walk :data:`COMPILE_STAGE_ORDER` in order. No model picks the path."""
        await asyncio.to_thread(self._db_load_checkpoints)
        for stage in COMPILE_STAGE_ORDER:
            self._check_deadline()
            await self._check_cancel()
            await asyncio.to_thread(self._db_set_active_stage, stage)
            self._notify("stage_started", stage=stage)
            await self._run_stage(stage)

    async def _run_stage(self, stage: str) -> None:
        handlers: dict[str, Callable[[], Awaitable[None]]] = {
            "intake": self._stage_intake,
            "research": self._stage_research,
            "outline": self._stage_outline,
            "draft": self._stage_draft,
            "lint": self._stage_lint,
            "copy": self._stage_copy_first_pass,
            "standards": self._stage_standards_first_pass,
            "fact": self._stage_fact_loop,
            "assemble": self._stage_assemble,
        }
        await handlers[stage]()

    # -- budgets, cancellation, checkpoints --------------------------------

    def _check_deadline(self) -> None:
        if self._deps.now() >= self._budget.deadline:
            raise CompileFailure(
                CODE_JOB_TIMEOUT,
                retryable=False,
                message="compile job exceeded its wall-clock budget",
            )

    def _check_model_call_budget(self) -> None:
        if self._budget.model_calls >= self._budget.max_model_calls:
            raise CompileFailure(
                CODE_MODEL_CALL_BUDGET_EXCEEDED,
                retryable=False,
                message="compile job exceeded its model-call budget",
            )

    async def _check_cancel(self) -> None:
        if await asyncio.to_thread(self._db_cancel_requested):
            raise _CompileCancelled()

    def _next_attempt(self, stage: str) -> int:
        attempt = self._attempts.get(stage, 0) + 1
        self._attempts[stage] = attempt
        return attempt

    def _reusable(self, stage: str, input_sha256: str) -> Optional["DraftStageRecord"]:
        """Return a checkpoint only when *everything* matches (SPEC §10.1.6).

        ``resume_allowed`` already gates the compile fingerprint, prompt bundle
        and source snapshot for the whole job; ``input_sha256`` then gates this
        specific stage, and because a stage's input hash folds in every
        upstream artifact hash, one re-run invalidates the whole downstream
        chain automatically. The artifact is additionally re-hashed here, so a
        row whose stored hash drifted from its stored JSON is never trusted.
        """
        if not self._ctx.resume_allowed:
            return None
        record = self._checkpoints.get(stage)
        if record is None or record.status != "completed":
            return None
        if record.input_sha256 != input_sha256:
            return None
        if record.artifact_sha256 != sha256_text(record.artifact_json):
            return None
        if record.content_md is not None and record.candidate_sha256 != sha256_text(
            record.content_md
        ):
            return None
        return record

    # -- model calls -------------------------------------------------------

    async def _call_model(
        self,
        *,
        stage: str,
        prompt: PromptDefinition,
        render: dict[str, Any],
        output_model: type[BaseModel],
    ) -> tuple[BaseModel, ModelCallAudit]:
        """One structured model call, with exactly one repair attempt.

        Order of gates on every call, without exception:
        deadline -> model-call budget -> cancellation -> provider allowlist ->
        call -> cancellation again (discarding an in-flight result) -> parse ->
        Pydantic validate. A validation failure buys exactly one repair call,
        which passes the schema error but never hidden reasoning (SPEC §14.3).
        """
        rendered = prompt.render(**render)
        raw = await self._provider_call(prompt, rendered)
        try:
            payload = _extract_json_object(raw)
            model = output_model.model_validate(payload)
        except (ValueError, ValidationError) as first_error:
            repair = (
                f"{rendered}\n\nREPAIR REQUEST: your previous response was not "
                f"valid for the required schema. Validation error:\n"
                f"{type(first_error).__name__}: {first_error}\n"
                "Return ONLY a corrected JSON object matching the schema above. "
                "Do not explain, and do not include any reasoning."
            )
            raw = await self._provider_call(prompt, repair)
            try:
                payload = _extract_json_object(raw)
                model = output_model.model_validate(payload)
            except (ValueError, ValidationError) as second_error:
                logger.warning(
                    "draft compile: stage %s produced invalid structured output "
                    "twice (%s)",
                    stage,
                    type(second_error).__name__,
                )
                raise CompileFailure(
                    CODE_INVALID_STAGE_OUTPUT,
                    retryable=False,
                    message=f"stage {stage} returned invalid structured output twice",
                ) from None
        audit = ModelCallAudit(
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
            model=_provider_base_url(prompt.logical_mode),
            temperature=prompt.temperature,
            output_sha256=sha256_text(raw or ""),
        )
        return model, audit

    async def _provider_call(self, prompt: PromptDefinition, rendered: str) -> str:
        """Guarded provider invocation with bounded transient auto-retry."""
        attempt = 0
        while True:
            self._check_deadline()
            self._check_model_call_budget()
            await self._check_cancel()

            base_url = _provider_base_url(prompt.logical_mode)
            # SPEC §9.2 / issue §6 law 13: the allowlist is re-checked before
            # EVERY call, so a mid-job policy revocation is honored.
            assert_provider_allowed(base_url, sensitive=self._ctx.sensitive)

            self._budget.model_calls += 1
            try:
                raw = await self._deps.complete(
                    rendered,
                    logical_mode=prompt.logical_mode,
                    temperature=prompt.temperature,
                    sensitive=self._ctx.sensitive,
                )
            except ProviderPolicyError as exc:
                # Never auto-retried: policy is a decision, not a fault.
                raise CompileFailure(
                    exc.code, retryable=False, message="provider policy rejected the call"
                ) from None
            except _CompileCancelled:
                raise
            except Exception as exc:
                if not _is_transient(exc) or attempt >= self._ctx.transient_retry_limit:
                    logger.warning(
                        "draft compile: provider call failed (%s)", type(exc).__name__
                    )
                    raise CompileFailure(
                        CODE_PROVIDER_UNAVAILABLE,
                        retryable=False,
                        message="provider call failed after bounded retries",
                    ) from None
                attempt += 1
                await asyncio.sleep(_backoff_seconds(attempt))
                continue

            # SPEC §10.2: an in-flight call may finish, but its output MUST be
            # discarded once cancellation has been observed.
            await self._check_cancel()
            await asyncio.to_thread(self._db_bump_model_calls, self._budget.model_calls)
            return raw

    # -- stage 0: intake ---------------------------------------------------

    async def _stage_intake(self) -> None:
        """Validate preconditions and snapshot the input manifest (SPEC §11.1)."""
        ctx = self._ctx
        input_sha = _stage_input_hash(
            "intake",
            ctx,
            {
                "inputs": [
                    [i.input_id, i.raw_sha256, i.parsed_sha256] for i in ctx.inputs
                ]
            },
        )
        reused = self._reusable("intake", input_sha)
        if reused is not None:
            self._manifest = IntakeManifest.model_validate_json(reused.artifact_json)
            return

        try:
            brief = json.loads(ctx.brief_json or "{}")
        except (TypeError, ValueError):
            raise CompileFailure(
                CODE_BRIEF_CONTRACT_FAILED, retryable=False
            ) from None
        required = {
            "piece_type",
            "audience",
            "purpose",
            "target_words",
            "transformation_strength",
        }
        if not isinstance(brief, dict) or not required.issubset(brief):
            raise CompileFailure(CODE_BRIEF_CONTRACT_FAILED, retryable=False)

        if not ctx.inputs:
            raise CompileFailure(CODE_INPUTS_NOT_READY, retryable=False)
        if ctx.mode == "rewrite" and not ctx.manuscript_inputs:
            raise CompileFailure(CODE_MANUSCRIPT_REQUIRED, retryable=False)
        if len(ctx.inputs) > settings.draft_max_inputs:
            raise CompileFailure(CODE_INPUT_LIMIT_EXCEEDED, retryable=False)
        total_chars = sum(i.character_count for i in ctx.inputs)
        if total_chars > settings.draft_max_total_parsed_chars:
            raise CompileFailure(CODE_INPUT_LIMIT_EXCEEDED, retryable=False)

        warnings: list[str] = []
        if not any(i.role == "reference" for i in ctx.inputs):
            warnings.append("no reference input supplied")

        manifest = IntakeManifest(
            brief_hash=ctx.brief_hash,
            inputs=[
                IntakeInputRecord(
                    input_id=i.input_id,
                    role=i.role,
                    raw_sha256=i.raw_sha256,
                    parsed_sha256=i.parsed_sha256,
                    character_count=i.character_count,
                )
                for i in ctx.inputs
            ],
            warnings=warnings,
        )
        self._manifest = manifest
        await self._persist_stage("intake", input_sha, manifest)

    # -- stage 1: research -------------------------------------------------

    def _research_brief(self) -> dict[str, Any]:
        """Decode the frozen brief snapshot into the mapping Research expects.

        ``CompileContext.brief_json`` is stored as a JSON *string* so the
        compile fingerprint hashes exact bytes. ``draft_research.run_research``
        documents ``brief: dict``, and its prompt builder json-dumps the value —
        passing the raw string would emit a JSON string-of-a-string rather than
        the brief object.
        """
        try:
            decoded = json.loads(self._ctx.brief_json or "{}")
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _research_inputs(self) -> list[dict[str, Any]]:
        """Marshal frozen ``_InputSnapshot`` rows into Research's dict contract.

        ``draft_research`` reads its inputs through ``.get()`` (``input_id``,
        ``role``, ``text``), so the dataclass snapshots cannot be handed over
        directly. Marshalling here keeps the pinned ``run_research(brief: dict,
        inputs: Sequence[dict], ...)`` signature intact and keeps the snapshot
        immutable on this side of the seam.
        """
        return [
            {
                "input_id": snap.input_id,
                "role": snap.role,
                "authority": snap.authority,
                "as_of_date": snap.as_of_date,
                "text": snap.parsed_text,
                "locked_spans": [list(span) for span in snap.locked_spans],
            }
            for snap in self._ctx.inputs
        ]

    async def _stage_research(self) -> None:
        """Delegate to ``draft_research`` and snapshot its evidence (SPEC §11.2)."""
        ctx = self._ctx
        if self._manifest is None:  # pragma: no cover - stage order guarantees this
            raise CompileFailure(CODE_INTERNAL_ERROR, retryable=False)
        input_sha = _stage_input_hash(
            "research", ctx, {"manifest": _artifact_json(self._manifest)}
        )
        reused = self._reusable("research", input_sha)
        if reused is not None:
            self._packet = ResearchPacket.model_validate_json(reused.artifact_json)
            await asyncio.to_thread(self._db_load_evidence_index)
            self._source_snapshot_sha256 = self._compute_source_snapshot()
            return

        run_research = _load_research_runner()
        try:
            outcome = await run_research(
                brief=self._research_brief(),
                inputs=self._research_inputs(),
                vault_id=ctx.vault_id,
                retrieve=self._deps.retrieve_sources,
                complete=self._research_complete,
                limit=ctx.retrieval_limit,
                retry_limit=ctx.transient_retry_limit,
            )
        except (CompileFailure, _CompileCancelled):
            raise
        except ProviderPolicyError as exc:
            raise CompileFailure(exc.code, retryable=False) from None
        except Exception as exc:
            logger.warning("draft compile: research failed (%s)", type(exc).__name__)
            raise CompileFailure(
                CODE_RETRIEVAL_UNAVAILABLE if _is_transient(exc) else CODE_INTERNAL_ERROR,
                retryable=False,
            ) from None

        blockers = tuple(getattr(outcome, "blockers", ()) or ())
        if blockers:
            raise CompileFailure(
                CODE_RESEARCH_BLOCKED,
                retryable=False,
                message="research reported a blocking gap",
            )

        packet: ResearchPacket = outcome.packet
        self._packet = packet
        await asyncio.to_thread(
            self._db_snapshot_evidence, tuple(getattr(outcome, "evidence", ()) or ())
        )
        self._source_snapshot_sha256 = self._compute_source_snapshot()
        await self._persist_stage("research", input_sha, packet)

    async def _research_complete(self, prompt: str, **kwargs: object) -> str:
        """Budget/policy-guarded ``complete`` handed to ``draft_research``.

        Research must not be able to bypass the budget, the allowlist or the
        cancellation check by holding a raw reference to ``deps.complete``.
        """
        definition = PROMPTS["research"]
        return await self._provider_call(definition, prompt)

    def _compute_source_snapshot(self) -> str:
        """Hash of the immutable evidence snapshot this job may cite.

        Part of the resume gate: if the snapshot changed, no downstream
        checkpoint may be reused.
        """
        packet = self._packet
        if packet is None:
            return ""
        return sha256_text(
            canonical_json(
                [[e.label, e.content_sha256] for e in sorted(
                    packet.evidence, key=lambda item: item.label
                )]
            )
        )

    def _evidence_registry(self) -> str:
        """Human-readable allowed-evidence registry injected into every prompt."""
        packet = self._packet
        if packet is None or not packet.evidence:
            return "(no vault evidence retrieved — source-only run)"
        return "\n".join(
            f"[{item.label}] {item.title}: {item.passage}" for item in packet.evidence
        )

    def _locked_spans_text(self) -> str:
        spans = [
            i.parsed_text[start:end]
            for i in self._ctx.inputs
            for start, end in i.locked_spans
            if 0 <= start < end <= len(i.parsed_text)
        ]
        return "\n".join(f"- {s}" for s in spans) if spans else "(none)"

    # -- stage 2: outline --------------------------------------------------

    async def _stage_outline(self) -> None:
        """Plan the sections and run the plan critic gate (SPEC §11.3)."""
        ctx = self._ctx
        if self._packet is None:  # pragma: no cover - stage order guarantees this
            raise CompileFailure(CODE_INTERNAL_ERROR, retryable=False)
        input_sha = _stage_input_hash(
            "outline",
            ctx,
            {
                "packet": sha256_text(_artifact_json(self._packet)),
                "sources": self._source_snapshot_sha256,
            },
        )
        reused = self._reusable("outline", input_sha)
        if reused is not None:
            self._outline = OutlineArtifact.model_validate_json(reused.artifact_json)
            self._enforce_section_budget(self._outline)
            return

        definition = PROMPTS["outline"]
        outline: Optional[OutlineArtifact] = None
        # SPEC §11.3: revise at most ``draft_qa_retry_limit`` times; a
        # ``rejected`` verdict fails immediately rather than looping forever.
        for _ in range(max(settings.draft_qa_retry_limit, 0) + 1):
            model, _audit = await self._call_model(
                stage="outline",
                prompt=definition,
                render=self._render_context(_artifact_json(self._packet)),
                output_model=OutlineArtifact,
            )
            outline = model  # type: ignore[assignment]
            if outline.critic.verdict == "rejected":
                raise CompileFailure(
                    CODE_OUTLINE_REJECTED,
                    retryable=False,
                    message="outline critic rejected the plan",
                )
            if outline.critic.verdict == "approved":
                break
        if outline is None:  # pragma: no cover - the loop runs at least once
            raise CompileFailure(CODE_INTERNAL_ERROR, retryable=False)
        if outline.critic.verdict != "approved":
            raise CompileFailure(
                CODE_OUTLINE_REJECTED,
                retryable=False,
                message="outline critic never approved the plan",
            )
        self._enforce_section_budget(outline)
        self._outline = outline
        await self._persist_stage("outline", input_sha, outline, prompt=definition)

    def _enforce_section_budget(self, outline: OutlineArtifact) -> None:
        if len(outline.sections) > self._ctx.max_sections:
            raise CompileFailure(
                CODE_SECTION_BUDGET_EXCEEDED,
                retryable=False,
                message="outline exceeded the section budget",
            )

    def _render_context(self, upstream: str, **extra: object) -> dict[str, Any]:
        """Placeholders every prompt template declares."""
        context: dict[str, Any] = {
            "brief": self._ctx.brief_json,
            "evidence_registry": self._evidence_registry(),
            "locked_spans": self._locked_spans_text(),
            "upstream_artifact": upstream,
            "continuity_text": "(none)",
        }
        context.update(extra)
        return context

    # -- stage 3: draft ----------------------------------------------------

    async def _stage_draft(self) -> None:
        """Generate one section at a time, in outline order (SPEC §11.4)."""
        ctx = self._ctx
        outline = self._outline
        if outline is None:  # pragma: no cover - stage order guarantees this
            raise CompileFailure(CODE_INTERNAL_ERROR, retryable=False)
        input_sha = _stage_input_hash(
            "draft",
            ctx,
            {
                "outline": sha256_text(_artifact_json(outline)),
                "sources": self._source_snapshot_sha256,
            },
        )
        reused = self._reusable("draft", input_sha)
        if reused is not None:
            self._draft_artifact = DraftArtifact.model_validate_json(
                reused.artifact_json
            )
            self._candidate = reused.content_md or ""
            return

        definition = PROMPTS["draft"]
        sections: list[DraftSection] = []
        previous_markdown = ""
        for entry in outline.sections:
            # SPEC §10.2: check cancellation between section calls.
            await self._check_cancel()
            self._check_deadline()
            allowed = {label for label in entry.evidence_labels}
            registry = "\n".join(
                f"[{item.label}] {item.title}: {item.passage}"
                for item in (self._packet.evidence if self._packet else [])
                if item.label in allowed
            )
            model, audit = await self._call_model(
                stage="draft",
                prompt=definition,
                render={
                    "brief": ctx.brief_json,
                    "evidence_registry": registry or "(no evidence for this section)",
                    "locked_spans": "\n".join(f"- {s}" for s in entry.must_preserve)
                    or "(none)",
                    "upstream_artifact": canonical_json(entry.model_dump(mode="json")),
                    "continuity_text": _paragraph_tail(previous_markdown) or "(none)",
                },
                output_model=DraftSection,
            )
            section: DraftSection = model  # type: ignore[assignment]
            section = section.model_copy(
                update={"section_id": entry.section_id, "model_call_audit": audit}
            )
            sections.append(section)
            previous_markdown = section.markdown

        artifact = DraftArtifact(sections=sections)
        # Concatenation order is the outline order; line endings are normalized
        # exactly once, here, before the first candidate hash (SPEC §11).
        candidate = _normalize_line_endings(
            "\n\n".join(s.markdown.strip() for s in sections if s.markdown.strip())
        )
        self._draft_artifact = artifact
        self._candidate = candidate
        await self._persist_stage(
            "draft", input_sha, artifact, candidate=candidate, prompt=definition
        )

    # -- stage 4: lint -----------------------------------------------------

    async def _stage_lint(self) -> None:
        """Deterministic lint plus at most N bounded rewrites (SPEC §11.5/§13)."""
        ctx = self._ctx
        input_sha = _stage_input_hash(
            "lint", ctx, {"candidate": sha256_text(self._candidate)}
        )
        reused = self._reusable("lint", input_sha)
        if reused is not None:
            self._candidate = reused.content_md or self._candidate
            self._collect_lint_findings(
                LintReport.model_validate_json(reused.artifact_json)
            )
            return

        report = run_deterministic_lint(self._candidate, rule_version=BOILERPLATE_RULE_VERSION)
        rewritten, applied = apply_bounded_rewrites(
            self._candidate, report, limit=settings.draft_lint_rewrite_limit
        )
        if applied:
            rewritten = _normalize_line_endings(rewritten)
            report = run_deterministic_lint(
                rewritten, rule_version=BOILERPLATE_RULE_VERSION
            )
            self._candidate = rewritten
        # SPEC §13.3: a residual blocker does NOT stop the pipeline — the draft
        # still lands in needs_review carrying a human-waivable finding.
        self._collect_lint_findings(report)
        await self._persist_stage("lint", input_sha, report, candidate=self._candidate)

    def _collect_lint_findings(self, report: LintReport) -> None:
        self._findings = [f for f in self._findings if f.stage != "lint"]
        for finding in report.findings:
            if finding.severity != "blocker" or finding.disposition != "open":
                continue
            self._findings.append(
                _PendingFinding(
                    stage="lint",
                    rule_id=finding.rule_id,
                    rule_version=report.rule_version,
                    category="boilerplate",
                    severity="blocker",
                    message=finding.message,
                    waivable=True,
                    original_text=finding.excerpt,
                    span_start=finding.start,
                    span_end=finding.end,
                )
            )

    # -- stages 5/6: copy and standards ------------------------------------

    async def _stage_copy_first_pass(self) -> None:
        await self._run_copy(reason="pre_fact")

    async def _stage_standards_first_pass(self) -> None:
        await self._run_standards(reason="pre_fact")

    async def _run_copy(self, *, reason: str) -> _TextResult:
        """Copy desk (SPEC §11.6). Always runs BEFORE Standards."""
        ctx = self._ctx
        input_sha = _stage_input_hash(
            "copy", ctx, {"candidate": sha256_text(self._candidate), "reason": reason}
        )
        reused = self._reusable("copy", input_sha)
        if reused is not None and reason == "pre_fact":
            report = CopyReport.model_validate_json(reused.artifact_json)
            self._candidate = reused.content_md or self._candidate
            return _TextResult(
                text=self._candidate,
                semantic_changed=bool(reused.semantic_changed),
                applied_edits=tuple(report.edits),
            )

        definition = PROMPTS["copy"]
        model, _audit = await self._call_model(
            stage="copy",
            prompt=definition,
            render=self._render_context(self._candidate),
            output_model=CopyReport,
        )
        report: CopyReport = model  # type: ignore[assignment]
        result = _apply_edits(self._candidate, report.edits)
        self._candidate = result.text
        applied_report = CopyReport(
            edits=list(result.applied_edits), findings=report.findings
        )
        await self._persist_stage(
            "copy",
            input_sha,
            applied_report,
            candidate=self._candidate,
            semantic_changed=result.semantic_changed,
            prompt=definition,
        )
        return result

    async def _run_standards(self, *, reason: str) -> _TextResult:
        """Standards desk (SPEC §11.7). Always runs AFTER Copy."""
        ctx = self._ctx
        input_sha = _stage_input_hash(
            "standards",
            ctx,
            {"candidate": sha256_text(self._candidate), "reason": reason},
        )
        reused = self._reusable("standards", input_sha)
        if reused is not None and reason == "pre_fact":
            report = StandardsReport.model_validate_json(reused.artifact_json)
            self._candidate = reused.content_md or self._candidate
            return _TextResult(
                text=self._candidate,
                semantic_changed=bool(reused.semantic_changed),
                applied_edits=tuple(report.edits),
            )

        definition = PROMPTS["standards"]
        model, _audit = await self._call_model(
            stage="standards",
            prompt=definition,
            render=self._render_context(self._candidate),
            output_model=StandardsReport,
        )
        report: StandardsReport = model  # type: ignore[assignment]
        result = _apply_edits(self._candidate, report.edits)
        self._candidate = result.text
        applied_report = StandardsReport(
            edits=list(result.applied_edits), findings=report.findings
        )
        await self._persist_stage(
            "standards",
            input_sha,
            applied_report,
            candidate=self._candidate,
            semantic_changed=result.semantic_changed,
            prompt=definition,
        )
        return result

    # -- stage 7: fact, and the bounded correction loop --------------------

    async def _stage_fact_loop(self) -> None:
        """Fact gate plus the bounded ``Fact -> Copy -> Standards -> Fact`` loop.

        Every iteration runs Fact against the candidate that Copy and Standards
        have *already* finished editing, which is what makes law 3 structural:
        a semantic edit can never be the last thing that happens to the bytes.

        Reaching the cap is NOT a failure. SPEC §11.8 is explicit: "The full
        correction loop may run at most ``draft_qa_retry_limit`` times.
        Residual issues become visible findings. An unresolved non-waivable
        blocker prevents Ready; it does not prevent storing the output as
        ``needs_review``." Failing here would discard an otherwise complete,
        Fact-verified draft and leave the author with nothing to inspect,
        which is the opposite of the findings/blockers/human-Ready design.

        The loop always exits immediately after a Fact run, so the candidate
        handed to Assemble is always Fact-verified even when findings remain
        unresolved. Those findings persist as rows, and the non-waivable ones
        are what the Ready endpoint refuses to approve.
        """
        while True:
            await self._stage_fact()
            if not self._fact_requires_correction():
                return
            if self._correction_loops >= self._ctx.max_correction_loops:
                # Cap reached: stop correcting and let the residual findings
                # speak for themselves. self._candidate is Fact-verified as of
                # the _stage_fact() call above, so Assemble's hash gate holds.
                logger.info(
                    "draft_pipeline: correction loop cap reached for job %s; "
                    "residual issues remain as findings",
                    self._ctx.job_id,
                )
                return
            self._correction_loops += 1
            reason = f"correction_{self._correction_loops}"
            # Copy first, Standards second. Always.
            await self._run_copy(reason=reason)
            await self._run_standards(reason=reason)
            # Any semantic change either desk just made invalidates the Fact
            # result above; the loop therefore returns to Fact, never onward.
            self._fact_report = None
            self._fact_candidate_sha256 = None

    async def _stage_fact(self) -> None:
        """Run Fact against an IMMUTABLE candidate (SPEC §11.8).

        The candidate is a local ``str``; it is re-hashed after the desk runs
        and any difference is a hard failure rather than a silent acceptance.
        """
        ctx = self._ctx
        candidate = self._candidate
        candidate_sha = sha256_text(candidate)
        input_sha = _stage_input_hash(
            "fact",
            ctx,
            {
                "candidate": candidate_sha,
                "sources": self._source_snapshot_sha256,
                "loop": self._correction_loops,
            },
        )
        reused = self._reusable("fact", input_sha)
        if reused is not None and reused.candidate_sha256 == candidate_sha:
            report = FactReport.model_validate_json(reused.artifact_json)
            self._fact_report = report
            self._fact_candidate_sha256 = candidate_sha
            self._claims = await self._resolve_claims(report, candidate)
            return

        definition = PROMPTS["fact"]
        model, _audit = await self._call_model(
            stage="fact",
            prompt=definition,
            render=self._render_context(candidate),
            output_model=FactReport,
        )
        report: FactReport = model  # type: ignore[assignment]

        # Law 4: Fact never mutates prose.
        if self._candidate != candidate or sha256_text(self._candidate) != candidate_sha:
            raise CompileFailure(
                CODE_FACT_MUTATED_CANDIDATE,
                retryable=False,
                message="the fact desk changed the candidate bytes",
            )

        self._claims = await self._resolve_claims(report, candidate)
        self._fact_report = report
        self._fact_candidate_sha256 = candidate_sha
        await self._persist_stage(
            "fact", input_sha, report, candidate=candidate, prompt=definition
        )
        if sha256_text(self._candidate) != candidate_sha:
            raise CompileFailure(CODE_FACT_MUTATED_CANDIDATE, retryable=False)

    def _fact_requires_correction(self) -> bool:
        return any(
            claim.status in _CORRECTABLE_CLAIM_STATUSES for claim in self._claims
        )

    async def _resolve_claims(
        self, report: FactReport, candidate: str
    ) -> list[_ClaimRow]:
        """Turn model claims into verifiable ledger rows.

        Deterministic Python — not model trust — enforces every SPEC §12.3/§12.4
        rule here:

        * a sourced classification whose evidence label is not in the immutable
          snapshot is downgraded to ``unsupported``; a fabricated supporting
          passage can therefore never be recorded;
        * ``unsupported`` claims get a claim-specific retrieval, and the
          retrieval audit is recorded *even when nothing is found*;
        * a direct quote must match its pinned passage exactly (speaker and
          surrounding context included, since the whole passage is compared);
        * names, numbers, dates, obligations, safety and causal claims and
          direct quotes are marked high-stakes;
        * a lone supporting source on a high-stakes claim raises
          ``single_source_warning`` and never manufactures corroboration.
        """
        rows: list[_ClaimRow] = []
        self._findings = [f for f in self._findings if f.stage != "fact"]
        ordinal = 0
        for claim in report.claims:
            span_start = candidate.find(claim.proposition)
            if span_start < 0 or not claim.proposition:
                # No verifiable span: record the gap, never invent one.
                self._findings.append(
                    _PendingFinding(
                        stage="fact",
                        rule_id="fact.claim_span_unresolved",
                        rule_version=PROMPT_BUNDLE_VERSION,
                        category="operational",
                        severity="warning",
                        message=(
                            "a reported claim could not be located in the candidate "
                            "text and was not recorded"
                        ),
                        waivable=True,
                    )
                )
                continue
            span_end = span_start + len(claim.proposition)

            status = claim.status
            labels = [
                label for label in claim.evidence_labels if label in self._evidence_ids
            ]
            unknown_labels = [
                label
                for label in claim.evidence_labels
                if label not in self._evidence_ids
            ]
            audit: Optional[RetrievalAudit] = claim.retrieval_audit

            if status in _SOURCED_CLAIM_STATUSES and not labels:
                # Claimed a sourced verdict with no snapshotted passage.
                status = "unsupported"
            if unknown_labels:
                self._findings.append(
                    _PendingFinding(
                        stage="fact",
                        rule_id="fact.evidence_label_not_snapshotted",
                        rule_version=PROMPT_BUNDLE_VERSION,
                        category="factuality",
                        severity="blocker",
                        message=(
                            "a claim cited an evidence label that is not in this "
                            "job's immutable evidence snapshot"
                        ),
                        waivable=False,
                        span_start=span_start,
                        span_end=span_end,
                    )
                )

            sources: list[tuple[str, str, str, Optional[float]]] = []
            quote_mismatch = False
            for label in labels:
                passage = self._evidence_passages.get(label, "")
                relationship = (
                    "contradicts" if status == "contradicted" else
                    "supports" if status == "supported" else "context"
                )
                quote = self._pin_quote(claim, candidate, passage)
                if quote is None:
                    quote_mismatch = quote_mismatch or claim.claim_type == "quote"
                    continue
                overlap = calculate_citation_lexical_overlap(
                    candidate, {label: passage}
                ).get(label)
                sources.append((label, relationship, quote, overlap))

            if claim.claim_type == "quote" and (quote_mismatch or not sources):
                status = "unsupported"
                self._findings.append(
                    _PendingFinding(
                        stage="fact",
                        rule_id="fact.quote_mismatch",
                        rule_version=PROMPT_BUNDLE_VERSION,
                        category="quote",
                        severity="blocker",
                        message=(
                            "a direct quotation does not match the snapshotted "
                            "source passage exactly"
                        ),
                        waivable=False,
                        span_start=span_start,
                        span_end=span_end,
                    )
                )

            if status == "unsupported":
                # Claim-specific retrieval, audited whether or not it finds
                # anything (SPEC §12.3).
                audit = await self._claim_retrieval_audit(claim)
                sources = []

            high_stakes = _is_high_stakes(claim)
            single_source = bool(
                high_stakes
                and len([s for s in sources if s[1] == "supports"]) == 1
            )
            if single_source:
                # SPEC §12.5 rule 4: tiers may tighten corroboration but never
                # make a known defect acceptable. standard -> warning;
                # high_stakes -> waivable blocker needing explicit attribution;
                # sensitive -> non-waivable unless the sole source is the
                # primary official authority for this exact proposition.
                sole_authority = self._sole_source_is_primary_authority(sources)
                severity_ss, waivable_ss, detail = _single_source_policy(
                    self._ctx.tier, sole_authority=sole_authority
                )
                self._findings.append(
                    _PendingFinding(
                        stage="fact",
                        rule_id="fact.single_source_high_stakes",
                        rule_version=PROMPT_BUNDLE_VERSION,
                        category="factuality",
                        severity=severity_ss,
                        message=(
                            "a high-stakes claim rests on a single source; "
                            "corroboration was not fabricated"
                            f" ({detail})"
                        ),
                        waivable=waivable_ss,
                        span_start=span_start,
                        span_end=span_end,
                    )
                )

            severity = (
                "blocker"
                if status in _BLOCKING_CLAIM_STATUSES
                else "warning"
                if high_stakes
                else "info"
            )
            if status in _BLOCKING_CLAIM_STATUSES:
                self._findings.append(
                    _PendingFinding(
                        stage="fact",
                        rule_id=f"fact.claim_{status}",
                        rule_version=PROMPT_BUNDLE_VERSION,
                        category="factuality",
                        severity="blocker",
                        message=(
                            f"an atomic claim is {status}; qualify, attribute, "
                            "remove, or evidence it before approval"
                        ),
                        waivable=False,
                        span_start=span_start,
                        span_end=span_end,
                    )
                )

            ordinal += 1
            rows.append(
                _ClaimRow(
                    ordinal=ordinal,
                    claim_text=claim.proposition,
                    span_start=span_start,
                    span_end=span_end,
                    claim_type=claim.claim_type,
                    status=status,
                    severity=severity,
                    rationale="",
                    retrieval_audit_json=(
                        canonical_json(audit.model_dump(mode="json"))
                        if audit is not None
                        else "{}"
                    ),
                    sources=tuple(sources),
                )
            )
        return rows

    def _pin_quote(
        self, claim: FactClaim, candidate: str, passage: str
    ) -> Optional[str]:
        """Return the exact snapshotted text this classification is pinned to.

        For a direct quote the quoted run from the candidate must be extractable
        verbatim from the passage (whitespace collapse is the only permitted
        normalization, per ``draft_store.validate_exact_quote``). For any other
        sourced claim the pinned text is the passage itself, so the ledger
        always points at real snapshotted bytes.
        """
        if not passage:
            return None
        if claim.claim_type == "quote":
            for quoted in _quoted_spans(claim.proposition) or _quoted_spans(candidate):
                try:
                    validate_exact_quote(passage, quoted)
                except DraftValidationError:
                    # This candidate span is not a verbatim match; try the next.
                    continue
                return quoted
            return None
        return passage

    async def _claim_retrieval_audit(self, claim: FactClaim) -> RetrievalAudit:
        """Claim-specific retrieval whose audit is recorded even on zero results."""
        ctx = self._ctx
        normalized = " ".join(claim.proposition.split())
        retrieved_at = self._deps.now().isoformat()
        config = canonical_json(
            {
                "limit": ctx.retrieval_limit,
                "source_kinds": ["document", "kms", "wiki"],
            }
        )
        try:
            result = await self._deps.retrieve_sources(
                normalized, ctx.vault_id, limit=ctx.retrieval_limit
            )
        except ProviderPolicyError as exc:
            raise CompileFailure(exc.code, retryable=False) from None
        except Exception as exc:
            # SPEC §20: never convert a retrieval error into an empty success.
            logger.warning(
                "draft compile: claim retrieval failed (%s)", type(exc).__name__
            )
            raise CompileFailure(CODE_RETRIEVAL_UNAVAILABLE, retryable=False) from None

        sources = tuple(getattr(result, "sources", ()) or ())
        return RetrievalAudit(
            normalized_query=normalized,
            vault_scope_hash=sha256_text(f"vault:{ctx.vault_id}"),
            retrieval_config=config,
            retrieved_at=retrieved_at,
            returned_labels=[
                s.content_sha256 for s in sources if getattr(s, "content_sha256", None)
            ],
            nearest_context=None,
        )

    # -- stage 8: assemble -------------------------------------------------

    async def _stage_assemble(self) -> None:
        """Byte-preserving final assembly into ``needs_review`` (SPEC §11.9)."""
        if PROVISIONAL_ASSEMBLY_ENABLED:  # pragma: no cover - permanently False
            raise CompileFailure(CODE_ASSEMBLE_WITHOUT_FACT, retryable=False)
        if self._fact_report is None or self._fact_candidate_sha256 is None:
            # No completed Fact stage => no revision. This is what disables the
            # SPEC §11.10 provisional path for every new job.
            raise CompileFailure(
                CODE_ASSEMBLE_WITHOUT_FACT,
                retryable=False,
                message="assemble requires a successful fact candidate",
            )

        candidate = self._candidate
        candidate_sha = sha256_text(candidate)
        if candidate_sha != self._fact_candidate_sha256:
            raise CompileFailure(
                CODE_ASSEMBLE_HASH_MISMATCH,
                retryable=False,
                message="assemble input does not match the successful fact candidate",
            )

        # Step 1: validate WITHOUT modifying content. Anything that would
        # require a mutation invalidates Fact instead of being silently fixed.
        allowed_labels = set(self._evidence_ids)
        cited = {
            f"{m.group(1)}{m.group(2)}" for m in _CITATION_LABEL_RE.finditer(candidate)
        }
        unknown = sorted(cited - allowed_labels)
        traces = [m for m in _REASONING_TRACE_MARKERS if m in candidate]
        if unknown or traces:
            raise CompileFailure(
                CODE_ASSEMBLE_VALIDATION_REQUIRES_MUTATION,
                retryable=False,
                message="candidate needs a prose mutation assemble may not make",
            )

        # Step 2: every claim span must still map to the byte-identical text.
        for row in self._claims:
            if candidate[row.span_start : row.span_end] != row.claim_text:
                raise CompileFailure(
                    CODE_ASSEMBLE_HASH_MISMATCH,
                    retryable=False,
                    message="a claim span no longer maps to the candidate text",
                )

        await self._check_cancel()

        fact_status = (
            "findings"
            if any(f.severity == "blocker" for f in self._findings)
            else "passed"
        )
        qa_summary = {
            "correction_loops": self._correction_loops,
            "model_calls": self._budget.model_calls,
            "prompt_bundle_version": self._ctx.prompt_bundle_version,
            "boilerplate_rule_version": BOILERPLATE_RULE_VERSION,
            "blockers": sum(1 for f in self._findings if f.severity == "blocker"),
            "source_only": bool(self._packet.source_only) if self._packet else False,
        }
        # Steps 3-5: one BEGIN IMMEDIATE transaction creates the immutable
        # revision, makes it current, points the job at it and moves the draft
        # to needs_review. NEVER ready.
        revision_id = await asyncio.to_thread(
            self._db_commit_revision,
            candidate,
            candidate_sha,
            fact_status,
            canonical_json(qa_summary),
        )
        # Step 4 (continued): claims, source links and findings hang off the
        # committed revision.
        await asyncio.to_thread(self._db_write_ledger, revision_id, candidate)

        input_sha = _stage_input_hash(
            "assemble",
            self._ctx,
            {"candidate": candidate_sha, "fact": self._fact_candidate_sha256},
        )
        artifact = {
            "revision_id": revision_id,
            "candidate_sha256": candidate_sha,
            "fact_status": fact_status,
            "qa_summary": qa_summary,
        }
        await self._persist_stage_raw(
            "assemble", input_sha, canonical_json(artifact), candidate=candidate
        )

    # -- persistence -------------------------------------------------------

    async def _persist_stage(
        self,
        stage: str,
        input_sha256: str,
        artifact: BaseModel,
        *,
        candidate: Optional[str] = None,
        semantic_changed: bool = False,
        prompt: Optional[PromptDefinition] = None,
    ) -> _StageOutcome:
        return await self._persist_stage_raw(
            stage,
            input_sha256,
            _artifact_json(artifact),
            candidate=candidate,
            semantic_changed=semantic_changed,
            prompt=prompt,
        )

    async def _persist_stage_raw(
        self,
        stage: str,
        input_sha256: str,
        artifact_json: str,
        *,
        candidate: Optional[str] = None,
        semantic_changed: bool = False,
        prompt: Optional[PromptDefinition] = None,
    ) -> _StageOutcome:
        """Write the stage row BEFORE the orchestrator advances (SPEC §10.1.5)."""
        attempt = self._next_attempt(stage)
        artifact_sha256 = sha256_text(artifact_json)
        await asyncio.to_thread(
            self._db_record_stage,
            stage,
            attempt,
            input_sha256,
            artifact_json,
            artifact_sha256,
            candidate,
            semantic_changed,
            prompt,
        )
        # SPEC §8.4: notify AFTER the stage transaction commits. Content-free —
        # ids, stage name, attempt and progress only, never manuscript text,
        # evidence passages, prompt bodies or exception detail.
        self._notify(
            "stage_completed",
            stage=stage,
            attempt=attempt,
            progress_percent=self._stage_progress(stage),
        )
        return _StageOutcome(
            artifact_json=artifact_json, artifact_sha256=artifact_sha256, reused=False
        )

    def _stage_progress(self, stage: str) -> float:
        """Completion percentage after ``stage``, from its position in the order."""
        try:
            index = COMPILE_STAGE_ORDER.index(stage) + 1
        except ValueError:  # pragma: no cover - stage names are a fixed tuple
            return 0.0
        return round(100.0 * index / len(COMPILE_STAGE_ORDER), 2)

    def _notify(self, event_type: str, **fields: Any) -> None:  # noqa: D401
        """Publish one content-free SSE notification; never raises (SPEC §8.4).

        SSE is notification only — canonical status always comes from SQLite —
        so a failing or absent subscriber must never affect the compile.
        """
        from app.services.draft_events import (
            DraftEventPayloadError as _DraftEventPayloadError,
        )

        try:
            self._deps.publish(
                event_type,
                draft_id=self._ctx.draft_id,
                job_id=self._ctx.job_id,
                **fields,
            )
        except _DraftEventPayloadError:
            # A payload-contract violation is a BUG, not a transient: the event
            # is silently lost for every subscriber. Log at error so it cannot
            # hide in warning noise the way an unallowlisted field once did.
            logger.error(
                "draft compile: stage event REJECTED by the payload allowlist "
                "(job_id=%s event=%s) — the event was not delivered",
                self._ctx.job_id,
                event_type,
                exc_info=True,
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "draft compile: stage event publish failed (job_id=%s event=%s)",
                self._ctx.job_id,
                event_type,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Synchronous DB steps. Each opens ONE pooled connection, commits, and
    # releases it. None contains an ``await``; none stores a connection.
    # ------------------------------------------------------------------

    def _db_load_checkpoints(self) -> None:
        with self._pool.connection() as conn:
            stages = DraftStore(conn).list_stages(job_id=self._ctx.job_id, limit=500)
        for record in stages:
            self._attempts[record.stage] = max(
                self._attempts.get(record.stage, 0), record.attempt
            )
            if record.status == "completed":
                self._checkpoints[record.stage] = record

    def _db_set_active_stage(self, stage: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE draft_jobs SET active_stage = ?, "
                "heartbeat_at = CURRENT_TIMESTAMP, progress_percent = ? WHERE id = ?",
                (
                    stage,
                    round(
                        100.0 * COMPILE_STAGE_ORDER.index(stage)
                        / len(COMPILE_STAGE_ORDER),
                        2,
                    ),
                    self._ctx.job_id,
                ),
            )
            conn.commit()

    def _db_cancel_requested(self) -> bool:
        with self._pool.connection() as conn:
            return DraftStore(conn).is_cancel_requested(self._ctx.job_id)

    def _db_bump_model_calls(self, count: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE draft_jobs SET model_call_count = ?, "
                "heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?",
                (count, self._ctx.job_id),
            )
            conn.commit()

    def _db_record_stage(
        self,
        stage: str,
        attempt: int,
        input_sha256: str,
        artifact_json: str,
        artifact_sha256: str,
        candidate: Optional[str],
        semantic_changed: bool,
        prompt: Optional[PromptDefinition],
    ) -> None:
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            row_id = store.record_stage_start(
                job_id=self._ctx.job_id,
                stage=stage,
                attempt=attempt,
                input_sha256=input_sha256,
            )
            store.record_stage_success(
                stage_row_id=row_id,
                artifact_json=artifact_json,
                artifact_sha256=artifact_sha256,
                content_md=candidate,
                candidate_sha256=(
                    sha256_text(candidate) if candidate is not None else None
                ),
                semantic_changed=semantic_changed,
                prompt_id=prompt.prompt_id if prompt else None,
                prompt_version=prompt.version if prompt else None,
                prompt_sha256=prompt.sha256 if prompt else None,
                model_name=(
                    _provider_base_url(prompt.logical_mode) if prompt else None
                ),
                temperature=prompt.temperature if prompt else None,
            )

    def _sole_source_is_primary_authority(
        self, sources: Sequence[tuple[Any, ...]]
    ) -> bool:
        """True when the single supporting source is a primary/official one.

        SPEC §12.5 rule 4's ``sensitive`` carve-out: a lone source may stand
        only when it is "the primary official authority for that exact
        proposition". The evidence snapshot records that judgement in its
        ``authority`` column, so read it from the snapshot rather than
        inferring it, and default to False (strict) whenever it cannot be
        established.
        """
        supporting = [s for s in sources if len(s) > 1 and s[1] == "supports"]
        if len(supporting) != 1:
            return False
        label = supporting[0][0]
        authority = self._evidence_authorities.get(label)
        return authority in ("primary", "official")

    def _db_snapshot_evidence(self, snapshots: tuple[object, ...]) -> None:
        """Persist the research evidence snapshot for this job.

        ``source_content_sha256`` must be the CANONICAL WHOLE-SOURCE hash
        (SPEC §12.6), because that is what evidence-freshness re-resolution
        compares against. The retrieval seam only knows the passage it
        returned, so ``draft_research`` fills the field with a passage hash;
        left as-is, every freshness check on a document, KMS entry or
        page-level Wiki row would report a spurious ``evidence_changed`` and
        Ready would be permanently unreachable. Resolve the real source hash
        here, where a connection is already open, and fall back to the
        snapshot value only when the source cannot be resolved.
        """
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            for snapshot in snapshots:
                kwargs = _evidence_kwargs(snapshot)
                canonical = _canonical_source_sha256_for(
                    conn,
                    kwargs,
                    draft_id=self._ctx.draft_id,
                    vault_id=self._ctx.vault_id,
                )
                if canonical:
                    kwargs["source_content_sha256"] = canonical
                evidence_id = store.insert_evidence(job_id=self._ctx.job_id, **kwargs)
                self._evidence_ids[kwargs["label"]] = evidence_id
                self._evidence_passages[kwargs["label"]] = kwargs["passage"]
                self._evidence_authorities[kwargs["label"]] = kwargs.get(
                    "authority"
                ) or "unknown"

    def _db_load_evidence_index(self) -> None:
        with self._pool.connection() as conn:
            rows = DraftStore(conn).list_evidence(job_id=self._ctx.job_id, limit=1000)
        for row in rows:
            self._evidence_ids[row.label] = row.id
            self._evidence_passages[row.label] = row.passage
            self._evidence_authorities[row.label] = row.authority or "unknown"

    def _db_commit_revision(
        self,
        content_md: str,
        content_sha256: str,
        fact_status: str,
        qa_summary_json: str,
    ) -> int:
        """Create the immutable revision and land the draft in ``needs_review``.

        One ``BEGIN IMMEDIATE`` transaction, mirroring
        ``DraftStore.create_manual_revision``: clear the old current flag,
        allocate ``MAX(revision_no)+1``, insert, mark current, point the job at
        it, and move the draft. The only status string written here is
        ``needs_review`` — ``ready`` appears nowhere in this module, so no
        automatic path can set it (SPEC §12.5 rule 8).
        """
        ctx = self._ctx
        with self._pool.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM drafts WHERE id = ? AND created_by = ?",
                    (ctx.draft_id, ctx.owner_id),
                ).fetchone()
                if row is None:
                    raise CompileFailure(CODE_INVALID_COMPILE_JOB, retryable=False)
                if row[0] not in _ASSEMBLE_ALLOWED_PRIOR_DRAFT_STATUSES:
                    raise CompileFailure(
                        CODE_INVALID_COMPILE_JOB,
                        retryable=False,
                        message="draft is not in a state assemble may complete",
                    )
                current = conn.execute(
                    "SELECT id FROM draft_revisions WHERE draft_id = ? AND "
                    "is_current = 1",
                    (ctx.draft_id,),
                ).fetchone()
                current_id = None if current is None else int(current[0])
                if current_id is not None:
                    conn.execute(
                        "UPDATE draft_revisions SET is_current = 0 WHERE id = ?",
                        (current_id,),
                    )
                next_no = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM "
                        "draft_revisions WHERE draft_id = ?",
                        (ctx.draft_id,),
                    ).fetchone()[0]
                )
                cur = conn.execute(
                    "INSERT INTO draft_revisions (draft_id, parent_revision_id, "
                    "job_id, revision_no, source, content_md, content_sha256, "
                    "sections_json, citations_json, qa_summary_json, fact_status, "
                    "is_current, created_by) "
                    "VALUES (?, ?, ?, ?, 'pipeline', ?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        ctx.draft_id,
                        current_id,
                        ctx.job_id,
                        next_no,
                        content_md,
                        content_sha256,
                        canonical_json(
                            [
                                s.section_id
                                for s in (
                                    self._draft_artifact.sections
                                    if self._draft_artifact
                                    else []
                                )
                            ]
                        ),
                        canonical_json(sorted(self._evidence_ids)),
                        qa_summary_json,
                        fact_status,
                        ctx.owner_id,
                    ),
                )
                revision_id = int(cur.lastrowid)
                conn.execute(
                    "UPDATE drafts SET status = 'needs_review', "
                    "ready_revision_id = NULL, ready_by = NULL, ready_at = NULL, "
                    "lock_version = lock_version + 1, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (ctx.draft_id,),
                )
                conn.execute(
                    "UPDATE draft_jobs SET output_revision_id = ? WHERE id = ?",
                    (revision_id, ctx.job_id),
                )
                conn.execute(
                    "INSERT INTO draft_events (draft_id, job_id, revision_id, "
                    "actor_user_id, event_type, event_json) "
                    "VALUES (?, ?, ?, ?, 'revision_created', ?)",
                    (
                        ctx.draft_id,
                        ctx.job_id,
                        revision_id,
                        ctx.owner_id,
                        canonical_json({"source": "pipeline", "revision_no": next_no}),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return revision_id

    def _db_write_ledger(self, revision_id: int, candidate: str) -> None:
        """Persist claims, claim/source links and findings for the revision."""
        with self._pool.connection() as conn:
            store = DraftStore(conn)
            for row in self._claims:
                claim_id = store.insert_claim(
                    revision_id=revision_id,
                    ordinal=row.ordinal,
                    claim_text=row.claim_text,
                    span_start=row.span_start,
                    span_end=row.span_end,
                    claim_type=row.claim_type,
                    status=row.status,
                    severity=row.severity,
                    rationale=row.rationale,
                    retrieval_audit_json=row.retrieval_audit_json,
                )
                for label, relationship, quote, overlap in row.sources:
                    evidence_id = self._evidence_ids.get(label)
                    if evidence_id is None:
                        continue
                    try:
                        store.link_claim_source(
                            claim_id=claim_id,
                            evidence_id=evidence_id,
                            relationship=relationship,
                            exact_quote=quote,
                            lexical_overlap_score=overlap,
                        )
                    except (DraftValidationError, DraftConflictError):
                        # A link the store rejects (quote no longer extractable,
                        # duplicate relationship) is dropped rather than forced;
                        # the claim keeps its own recorded status. A real DB
                        # error still propagates.
                        continue
            for finding in self._findings:
                start, end = finding.span_start, finding.span_end
                if (
                    start is None
                    or end is None
                    or start < 0
                    or end <= start
                    or end > len(candidate)
                ):
                    start = end = None
                store.insert_finding(
                    draft_id=self._ctx.draft_id,
                    stage=finding.stage,
                    rule_id=finding.rule_id,
                    rule_version=finding.rule_version,
                    category=finding.category,
                    severity=finding.severity,
                    message=finding.message,
                    revision_id=revision_id,
                    job_id=self._ctx.job_id,
                    waivable=finding.waivable,
                    original_text=finding.original_text,
                    suggestion=finding.suggestion,
                    span_start=start,
                    span_end=end,
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_context(
    conn: sqlite3.Connection, job: "DraftJobRecord", now: datetime
) -> CompileContext:
    """Load the job's immutable world view in one connection, synchronously."""
    store = DraftStore(conn)
    draft = store.get_draft(job.draft_id, job.created_by)
    records = store.list_inputs(draft_id=job.draft_id, owner_id=job.created_by)

    snapshots: list[_InputSnapshot] = []
    for record in records:
        if record.parse_status != "ready":
            raise CompileFailure(CODE_INPUTS_NOT_READY, retryable=False)
        parsed = (
            store.get_input_parsed_text(
                draft_id=job.draft_id, owner_id=job.created_by, input_id=record.id
            )
            or ""
        )
        try:
            spans = json.loads(record.locked_spans_json or "[]")
        except (TypeError, ValueError):
            spans = []
        locked = tuple(
            (int(s["start"]), int(s["end"]))
            for s in spans
            if isinstance(s, dict) and "start" in s and "end" in s
        )
        snapshots.append(
            _InputSnapshot(
                input_id=record.id,
                role=record.role,
                authority=record.authority,
                as_of_date=record.as_of_date,
                raw_sha256=record.content_sha256,
                parsed_sha256=record.parsed_text_sha256 or sha256_text(parsed),
                character_count=record.parsed_char_count or len(parsed),
                parsed_text=parsed,
                locked_spans=locked,
            )
        )

    brief_hash = sha256_text(draft.brief_json or "")
    prior = store.get_current_revision(draft_id=job.draft_id, owner_id=job.created_by)
    # SPEC §10.1 item 6: the canonical compile fingerprint. Resume is permitted
    # only when the saved fingerprint still equals this value.
    fingerprint = sha256_text(
        canonical_json(
            {
                "brief_hash": brief_hash,
                "mode": draft.mode,
                "tier": draft.tier,
                "prompt_bundle_version": PROMPT_BUNDLE_VERSION,
                "prior_revision_sha256": prior.content_sha256 if prior else "",
                "inputs": [
                    [s.input_id, s.role, s.authority, s.raw_sha256, s.parsed_sha256]
                    for s in snapshots
                ],
            }
        )
    )
    timeout = job.timeout_seconds or settings.draft_job_timeout_seconds
    resume_allowed = bool(
        job.compile_input_sha256 == fingerprint
        and job.prompt_bundle_version == PROMPT_BUNDLE_VERSION
    )
    return CompileContext(
        job_id=job.id,
        draft_id=job.draft_id,
        owner_id=job.created_by,
        vault_id=job.vault_id,
        tier=draft.tier,
        mode=draft.mode,
        brief_json=draft.brief_json,
        brief_hash=brief_hash,
        inputs=tuple(snapshots),
        prompt_bundle_version=PROMPT_BUNDLE_VERSION,
        compile_fingerprint=fingerprint,
        start_stage=job.start_stage,
        started_at=now,
        deadline=now + timedelta(seconds=timeout),
        max_model_calls=(
            job.max_model_calls
            if job.max_model_calls > 0
            else settings.draft_job_max_model_calls
        ),
        max_sections=settings.draft_max_sections,
        max_correction_loops=settings.draft_qa_retry_limit,
        transient_retry_limit=settings.draft_transient_retry_limit,
        retrieval_limit=settings.draft_research_retrieval_limit,
        resume_allowed=resume_allowed,
    )


async def run_compile(
    *, job_id: int, pool: "SQLiteConnectionPool", deps: PipelineDeps
) -> None:
    """Run one ``compile`` job through the full editorial pipeline.

    This is the ONLY entry point ``DraftJobProcessor`` calls. The job must
    already be claimed (``pending -> running``) by the caller.

    On success the job is ``completed``, an immutable ``pipeline`` revision is
    current, and the draft is ``needs_review``.

    On failure the terminal state (job ``failed``/``cancelled``, draft
    ``failed``/``cancelled``) is persisted here **and then** the
    :class:`CompileFailure` is re-raised, so the caller can read ``code`` and
    ``retryable`` without re-deriving them from the database. ``retryable`` is
    True only for transient provider/retrieval faults; authorization,
    validation, content-size, provider-policy and budget failures are always
    False.

    Args:
        job_id: the claimed compile job.
        pool: the SQLite connection pool. A connection is opened per DB step
            and released before any ``await`` — never held across one.
        deps: model/retrieval/clock injection seam. Use :func:`default_deps`.

    Raises:
        CompileFailure: on any terminal pipeline failure, after the terminal
            state has been persisted.
    """
    now = deps.now()

    def _load_job() -> Optional["DraftJobRecord"]:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT draft_id, created_by FROM draft_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            return DraftStore(conn).get_job(
                draft_id=int(row[0]), owner_id=int(row[1]), job_id=job_id
            )

    job = await asyncio.to_thread(_load_job)
    if job is None or job.job_type != "compile":
        raise CompileFailure(
            CODE_INVALID_COMPILE_JOB,
            retryable=False,
            message="job is missing or is not a compile job",
        )

    def _context() -> CompileContext:
        with pool.connection() as conn:
            return _build_context(conn, job, now)

    try:
        ctx = await asyncio.to_thread(_context)
    except CompileFailure as failure:
        await asyncio.to_thread(
            _persist_failure, pool, job, failure.code, str(failure)
        )
        raise

    run = _CompileRun(pool=pool, deps=deps, ctx=ctx)
    try:
        await run.execute()
    except _CompileCancelled:
        await asyncio.to_thread(_persist_cancelled, pool, job)
        raise CompileFailure(
            CODE_JOB_CANCELLED, retryable=False, message="job cancellation observed"
        ) from None
    except CompileFailure as failure:
        await asyncio.to_thread(
            _persist_failure, pool, job, failure.code, str(failure)
        )
        raise
    except ProviderPolicyError as exc:
        await asyncio.to_thread(_persist_failure, pool, job, exc.code, "provider policy")
        raise CompileFailure(exc.code, retryable=False) from None
    except Exception as exc:
        logger.error(
            "draft compile: job id=%d raised %s", job_id, type(exc).__name__
        )
        await asyncio.to_thread(
            _persist_failure, pool, job, CODE_INTERNAL_ERROR, None
        )
        raise CompileFailure(CODE_INTERNAL_ERROR, retryable=False) from None

    await asyncio.to_thread(_persist_completed, pool, job)


def _persist_completed(pool: "SQLiteConnectionPool", job: "DraftJobRecord") -> None:
    with pool.connection() as conn:
        DraftStore(conn).set_job_status(
            job_id=job.id, target="completed", progress_percent=100.0
        )


def _persist_failure(
    pool: "SQLiteConnectionPool",
    job: "DraftJobRecord",
    code: str,
    message: Optional[str],
) -> None:
    """Settle job and draft onto ``failed`` with a stable, sanitized code."""
    with pool.connection() as conn:
        store = DraftStore(conn)
        try:
            store.set_job_status(
                job_id=job.id, target="failed", error_code=code, error_message=message
            )
        except Exception:
            logger.error("draft compile: could not fail job id=%d", job.id)
        _move_draft(conn, job.draft_id, "failed")


def _persist_cancelled(pool: "SQLiteConnectionPool", job: "DraftJobRecord") -> None:
    with pool.connection() as conn:
        store = DraftStore(conn)
        try:
            store.set_job_status(job_id=job.id, target="cancelled")
        except Exception:
            logger.error("draft compile: could not cancel job id=%d", job.id)
        _move_draft(conn, job.draft_id, "cancelled")


def _move_draft(conn: sqlite3.Connection, draft_id: int, target: str) -> None:
    """Move a compile-owned draft onto a terminal status (SPEC §10.3).

    ``target`` is restricted to the two terminal compile outcomes; ``ready`` is
    unreachable from this module by construction.
    """
    if target not in ("failed", "cancelled"):  # pragma: no cover - defensive
        raise ValueError("compile may only move a draft to failed or cancelled")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if row is not None and row[0] in ("queued", "running"):
            conn.execute(
                "UPDATE drafts SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (target, draft_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
