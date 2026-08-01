"""Draft Room Research stage (SPEC.md §11.2, §4.4, §12.1, §12.2).

Derives stable, deterministic research facets and candidate claims from a
draft's project inputs (honoring each input's role per SPEC §12.1), retrieves
vault evidence separately for each facet through the injected ``retrieve``
seam (``RAGEngine.retrieve_sources`` in production — never the private
``_execute_retrieval``), and snapshots the result into normalized,
label-stable :class:`EvidenceSnapshot` rows for the pipeline to persist via
``DraftStore.insert_evidence``.

This module never touches SQLite: it takes no ``conn`` and does no DB access
at all (INTERFACES.md hard rule). It also never converts a retrieval error
into empty evidence — a retrieval failure is always reflected in
``ResearchOutcome.retrieval_status``/``blockers``, never silently dropped.

Facets and evidence labels are computed deterministically from the retrieval
results alone, independent of the model call, so two runs against identical
inputs/retrieval produce identical facets, labels, and evidence snapshots
even though the model-derived ``contradictions``/``gaps`` may vary. Only the
``ResearchPacket.contradictions``/``gaps`` fields come from ``complete()``,
via ``PROMPTS["research"]`` (never a hand-rolled prompt) with one allowed
structured-output repair attempt (SPEC §14.3).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Sequence

from pydantic import ValidationError

from .draft_prompts import PROMPTS, ResearchEvidenceItem, ResearchFacet, ResearchPacket

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing rag_engine
    from .rag_engine import RAGRetrievalResult

# ---------------------------------------------------------------------------
# Pinned output types (INTERFACES.md W3-RESEARCH)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceSnapshot:
    """One normalized evidence row, ready for DraftStore.insert_evidence(**asdict)."""

    label: str
    source_kind: str
    file_id: int | None
    wiki_page_id: int | None
    wiki_claim_id: int | None
    kms_entry_id: int | None
    chunk_uid: str | None
    title: str
    passage: str
    source_content_sha256: str
    retrieval_score: float
    source_updated_at: str | None


@dataclass(frozen=True)
class ResearchOutcome:
    packet: ResearchPacket
    evidence: tuple[EvidenceSnapshot, ...]
    retrieval_status: Literal["ok", "partial", "unavailable"]
    source_only: bool
    blockers: tuple[str, ...]


class ResearchError(Exception):
    """Raised when the Research stage cannot produce a valid packet."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Facet derivation (SPEC §11.2 / §12.1 role table)
# ---------------------------------------------------------------------------

# Roles that may supply candidate facts/claims worth verifying against vault
# evidence (SPEC §12.1). ``style`` is deliberately excluded: it supplies
# prose characteristics only and "never evidence".
_FACET_ROLES: frozenset[str] = frozenset(
    {"manuscript", "reference", "background", "challenge"}
)

_ROLE_RATIONALE: dict[str, str] = {
    "manuscript": (
        "Candidate claim from the manuscript; verify against vault evidence."
    ),
    "reference": (
        "Reference material asserting facts; cite and verify, not "
        "automatically true."
    ),
    "background": "Candidate background context; verify before assertion.",
    "challenge": "Disputed claim; seek confirmation or contradiction.",
}

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_MAX_FACETS_PER_INPUT = 5
_MIN_SENTENCE_CHARS = 20
_FALLBACK_QUERY_CHARS = 300


def _input_id(input_record: dict) -> int:
    value = input_record.get("input_id", input_record.get("id"))
    return int(value)


def _input_text(input_record: dict) -> str:
    return (
        input_record.get("text")
        or input_record.get("parsed_text")
        or input_record.get("content")
        or ""
    )


def _split_candidate_sentences(text: str) -> list[str]:
    """Deterministically split ``text`` into bounded candidate-claim queries."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return []
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(collapsed) if s.strip()]
    substantive = [s for s in sentences if len(s) >= _MIN_SENTENCE_CHARS]
    if substantive:
        return substantive[:_MAX_FACETS_PER_INPUT]
    return [collapsed[:_FALLBACK_QUERY_CHARS]]


def _derive_facets(inputs: Sequence[dict]) -> list[ResearchFacet]:
    """Derive stable, deterministic facets from ``inputs``, honoring role.

    Ordered by input id, then by sentence order within an input, so the
    resulting facet list — and therefore every retrieval call and evidence
    label built from it — is identical across repeated runs on the same
    inputs.
    """
    facets: list[ResearchFacet] = []
    for input_record in sorted(inputs, key=_input_id):
        role = input_record.get("role")
        if role not in _FACET_ROLES:
            continue
        input_id = _input_id(input_record)
        rationale = _ROLE_RATIONALE[role]
        queries = _split_candidate_sentences(_input_text(input_record))
        for idx, query in enumerate(queries, start=1):
            facets.append(
                ResearchFacet(
                    facet_id=f"f-{role}-{input_id}-{idx}",
                    query=query,
                    source_input_ids=[input_id],
                    rationale=rationale,
                )
            )
    return facets


# ---------------------------------------------------------------------------
# Per-facet retrieval with bounded retry (SPEC §10.2)
# ---------------------------------------------------------------------------

_ALL_SOURCE_KINDS: frozenset[str] = frozenset({"document", "wiki", "kms"})
_SOURCE_KIND_ORDER: tuple[str, ...] = ("document", "wiki", "kms")
_RETRY_BACKOFF_SECONDS = 0.01


@dataclass(frozen=True)
class _FacetRetrieval:
    """Uniform shape covering both a real ``RAGRetrievalResult`` and the
    synthetic marker used when transient retries are exhausted."""

    sources: tuple
    requested_kinds: frozenset[str]
    successful_kinds: frozenset[str]
    failed_kinds: frozenset[str]


def _exhausted_retrieval() -> _FacetRetrieval:
    return _FacetRetrieval(
        sources=(),
        requested_kinds=_ALL_SOURCE_KINDS,
        successful_kinds=frozenset(),
        failed_kinds=_ALL_SOURCE_KINDS,
    )


async def _retrieve_facet(
    retrieve: Callable[..., Awaitable["RAGRetrievalResult"]],
    facet: ResearchFacet,
    vault_id: int,
    *,
    limit: int,
    retry_limit: int,
) -> _FacetRetrieval:
    """Call ``retrieve`` for one facet, retrying a raised (transient) failure
    at most ``retry_limit`` times with bounded backoff before surfacing it as
    a fully-failed retrieval for this facet — never as empty evidence."""
    attempt = 0
    while True:
        try:
            result = await retrieve(facet.query, vault_id, limit=limit)
            return _FacetRetrieval(
                sources=tuple(result.sources),
                requested_kinds=frozenset(result.requested_kinds),
                successful_kinds=frozenset(result.successful_kinds),
                failed_kinds=frozenset(result.failed_kinds),
            )
        except Exception:
            if attempt >= retry_limit:
                return _exhausted_retrieval()
            attempt += 1
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)


def _sorted_kinds(kinds: set[str]) -> list[str]:
    return [kind for kind in _SOURCE_KIND_ORDER if kind in kinds]


def _aggregate_kinds(
    facet_results: Sequence[_FacetRetrieval],
) -> tuple[set[str], set[str], set[str]]:
    requested: set[str] = set()
    successful: set[str] = set()
    failed: set[str] = set()
    for result in facet_results:
        requested |= result.requested_kinds
        successful |= result.successful_kinds
        failed |= result.failed_kinds
    return requested, successful, failed


def _label_evidence(
    facet_results: Sequence[_FacetRetrieval],
) -> list[EvidenceSnapshot]:
    """Assign stable S#/W#/K# labels in facet order (SPEC §12.2).

    KNOWN LIMITATION -- ``[D#]`` project-input labels are NOT minted here.

    Issue #436 §5 requires citation validation to *support* ``[D#]`` only when
    an explicit Draft registry is supplied, with an empty default so chat is
    unaffected; that is implemented and tested in
    ``citation_validator.calculate_citation_lexical_overlap`` /
    ``parse_draft_citations``. SPEC §12.2 additionally describes ``[D#]`` as
    part of the job's immutable evidence snapshot, and the supporting
    plumbing exists (the ``draft_input`` source kind, the
    ``draft_evidence.draft_input_id`` column, the freshness resolver for that
    family, and the ``D`` branch of the citation regex).

    Minting the labels here was implemented and then reverted: project inputs
    are always present, so counting them as evidence made ``source_only``
    permanently False and masked the genuine empty-vault state that SPEC §12.5
    rule 7 requires a human to acknowledge. Separating "vault evidence" from
    "input evidence" through the status logic, the prompts, the ledger and the
    Ready gate is a coherent change, but it is a broad semantic one and is not
    required by this issue's acceptance criteria. Until it is made, a model
    emitting ``[D1]`` has that label removed by the pre-Fact sanitation pass
    and surfaced as a finding, rather than silently presenting provenance the
    ledger cannot back.
    """
    doc_n = wiki_n = kms_n = 0
    evidence: list[EvidenceSnapshot] = []
    for result in facet_results:
        for source in result.sources:
            if source.kind == "document":
                doc_n += 1
                label = f"S{doc_n}"
            elif source.kind == "wiki":
                wiki_n += 1
                label = f"W{wiki_n}"
            elif source.kind == "kms":
                kms_n += 1
                label = f"K{kms_n}"
            else:
                raise ResearchError(
                    "invalid_stage_output",
                    f"unknown source kind {source.kind!r} from retrieval",
                )
            evidence.append(
                EvidenceSnapshot(
                    label=label,
                    source_kind=source.kind,
                    file_id=source.file_id,
                    wiki_page_id=source.wiki_page_id,
                    wiki_claim_id=source.wiki_claim_id,
                    kms_entry_id=source.kms_entry_id,
                    chunk_uid=source.chunk_uid,
                    title=source.title,
                    passage=source.passage,
                    source_content_sha256=source.content_sha256,
                    retrieval_score=source.score,
                    source_updated_at=source.updated_at,
                )
            )
    return evidence


# ---------------------------------------------------------------------------
# Structured-output model call (SPEC §14.1/§14.3) — contradictions/gaps only;
# facets/evidence/retrieval status are deterministic and never taken from the
# model's output.
# ---------------------------------------------------------------------------


def _build_research_prompt(
    *, brief: dict, inputs: Sequence[dict], evidence: Sequence[EvidenceSnapshot]
) -> str:
    brief_text = json.dumps(brief, ensure_ascii=False, sort_keys=True, indent=2)
    evidence_lines = [
        f"[{ev.label}] ({ev.source_kind}) {ev.title}\n{ev.passage}" for ev in evidence
    ]
    evidence_registry = (
        "\n\n".join(evidence_lines) if evidence_lines else "(no evidence retrieved)"
    )
    upstream_lines: list[str] = []
    locked_spans_lines: list[str] = []
    for input_record in sorted(inputs, key=_input_id):
        input_id = _input_id(input_record)
        role = input_record.get("role", "unknown")
        upstream_lines.append(
            f"[input {input_id} role={role}]\n{_input_text(input_record)}"
        )
        for span in input_record.get("locked_spans") or ():
            locked_spans_lines.append(f"input {input_id}: {span}")
    upstream_artifact = "\n\n".join(upstream_lines) if upstream_lines else "(no inputs)"
    locked_spans = "\n".join(locked_spans_lines) if locked_spans_lines else "(none)"
    return PROMPTS["research"].render(
        brief=brief_text,
        evidence_registry=evidence_registry,
        locked_spans=locked_spans,
        upstream_artifact=upstream_artifact,
    )


def _parse_packet(raw: str) -> ResearchPacket:
    data = json.loads(raw)
    return ResearchPacket.model_validate(data)


def _build_repair_prompt(original_prompt: str, invalid_output: str, error: Exception) -> str:
    return (
        f"{original_prompt}\n\n"
        "SCHEMA REPAIR: Your previous response was not valid JSON for the "
        "required schema. Return ONLY a corrected JSON object matching the "
        "schema above — no prose, no markdown fences, no chain-of-thought or "
        "explanation of the fix.\n\n"
        f"PREVIOUS INVALID OUTPUT:\n{invalid_output}\n\n"
        f"VALIDATION ERROR:\n{error}"
    )


async def _complete_packet(
    *,
    complete: Callable[..., Awaitable[str]],
    prompt: str,
) -> ResearchPacket:
    """Call the model, parse/validate its JSON, and allow exactly one repair
    attempt on failure (SPEC §14.3). A second invalid result raises."""
    prompt_def = PROMPTS["research"]
    raw = await complete(prompt, logical_mode=prompt_def.logical_mode, temperature=prompt_def.temperature)
    try:
        return _parse_packet(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        repair_prompt = _build_repair_prompt(prompt, raw, exc)
        raw2 = await complete(
            repair_prompt,
            logical_mode=prompt_def.logical_mode,
            temperature=prompt_def.temperature,
        )
        try:
            return _parse_packet(raw2)
        except (json.JSONDecodeError, ValidationError) as exc2:
            raise ResearchError(
                "invalid_stage_output",
                "research stage produced invalid structured output after one "
                "repair attempt",
            ) from exc2


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_research(
    *,
    brief: dict,
    inputs: Sequence[dict],
    vault_id: int,
    retrieve: Callable[..., Awaitable["RAGRetrievalResult"]],
    complete: Callable[..., Awaitable[str]],
    limit: int,
    retry_limit: int,
) -> ResearchOutcome:
    """Run the Research stage (SPEC §11.2).

    Retrieves each derived facet separately via ``retrieve`` (never
    ``rag_engine._execute_retrieval``), snapshots the results into
    label-stable :class:`EvidenceSnapshot` rows, and — only when at least one
    piece of evidence was retrieved — calls ``complete`` once (with one
    allowed structured-output repair) to derive ``contradictions``/``gaps``
    grounded in that evidence. Facets, evidence, and retrieval status are
    always deterministic and never overridden by the model's output.
    """
    facets = _derive_facets(inputs)

    facet_results = [
        await _retrieve_facet(
            retrieve, facet, vault_id, limit=limit, retry_limit=retry_limit
        )
        for facet in facets
    ]

    evidence = _label_evidence(facet_results)
    requested_kinds, successful_kinds, failed_kinds = _aggregate_kinds(facet_results)
    any_failed = bool(failed_kinds)

    if evidence:
        if any_failed:
            retrieval_status: Literal["ok", "partial", "unavailable"] = "partial"
            blockers: tuple[str, ...] = ("retrieval_partial",)
            source_only = False
        else:
            retrieval_status = "ok"
            blockers = ()
            source_only = False
    else:
        if any_failed:
            retrieval_status = "unavailable"
            blockers = ("retrieval_unavailable",)
            source_only = False
        else:
            retrieval_status = "ok"
            blockers = ()
            source_only = True

    requested_list = _sorted_kinds(requested_kinds)
    successful_list = _sorted_kinds(successful_kinds)
    failed_list = _sorted_kinds(failed_kinds)

    if not evidence:
        # Nothing retrieved (genuinely empty vault or full outage) — nothing
        # for the model to reason about, so skip the model call entirely
        # rather than risk it fabricating contradictions/gaps from nothing.
        packet = ResearchPacket(
            facets=facets,
            retrieval_status=retrieval_status,
            requested_source_kinds=requested_list,
            successful_source_kinds=successful_list,
            failed_source_kinds=failed_list,
            evidence=[],
            contradictions=[],
            gaps=[],
            source_only=source_only,
        )
        return ResearchOutcome(
            packet=packet,
            evidence=(),
            retrieval_status=retrieval_status,
            source_only=source_only,
            blockers=blockers,
        )

    evidence_items = [
        ResearchEvidenceItem(
            label=ev.label,
            kind=ev.source_kind,
            title=ev.title,
            passage=ev.passage,
            chunk_ref=ev.chunk_uid,
            observed_at=ev.source_updated_at,
            retrieval_score=ev.retrieval_score,
            content_sha256=ev.source_content_sha256,
            file_id=ev.file_id,
            chunk_uid=ev.chunk_uid,
            wiki_page_id=ev.wiki_page_id,
            wiki_claim_id=ev.wiki_claim_id,
            kms_entry_id=ev.kms_entry_id,
        )
        for ev in evidence
    ]

    prompt = _build_research_prompt(brief=brief, inputs=inputs, evidence=evidence)
    model_packet = await _complete_packet(complete=complete, prompt=prompt)

    packet = model_packet.model_copy(
        update={
            "facets": facets,
            "retrieval_status": retrieval_status,
            "requested_source_kinds": requested_list,
            "successful_source_kinds": successful_list,
            "failed_source_kinds": failed_list,
            "evidence": evidence_items,
            "source_only": source_only,
        }
    )
    return ResearchOutcome(
        packet=packet,
        evidence=tuple(evidence),
        retrieval_status=retrieval_status,
        source_only=source_only,
        blockers=blockers,
    )
