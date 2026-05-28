"""Optional LLM Wiki Curator (PR C).

Runs after deterministic extraction inside a wiki compile job. Goals:

  * High-recall candidate generation: a small instruct model proposes
    structured claims/entities/relations that the deterministic
    extractor missed.
  * Trust boundary on provenance: a candidate is only allowed to land
    as an active wiki claim when its source_quote is verifiable in the
    chunk it cites (substring or rapidfuzz partial_ratio above
    threshold) AND the chunk_id is one we actually fed to the curator.
  * Failure isolation: any curator error (timeout, breaker open,
    unparseable JSON, schema violation) is recorded into the job's
    ``result_json.curator.errors`` list. The deterministic pipeline's
    output is the final word; curator output never replaces it.

Concurrency: per-chunk calls run in parallel up to
``settings.wiki_llm_curator_concurrency`` (1-4). With concurrency=1
the calls are serialised. Each call sees a single chunk plus the
deterministic candidate summary; results are merged + de-duped at the
end. This honours the operator-facing knob exposed in PR B settings.

The compiler integration in wiki_compiler.py is responsible for
deciding *whether* to run the curator (settings flags
``wiki_llm_curator_enabled`` + ``wiki_llm_curator_run_on_*``). This
module just runs the curator when asked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.services.curator_ssrf import CuratorURLBlocked
from app.services.wiki_curator_client import CuratorClient, extract_json
from app.services.wiki_store import WikiStore

logger = logging.getLogger(__name__)


# A single chunk handed to the curator. The compiler prepares these
# from already-loaded source rows.
@dataclass
class CuratorChunk:
    chunk_id: str
    source_text: str
    file_id: Optional[int] = None
    source_label: Optional[str] = None


@dataclass
class CuratorAcceptedClaim:
    claim_text: str
    claim_type: str
    subject: Optional[str]
    predicate: Optional[str]
    object: Optional[str]
    source_quote: str
    chunk_id: str
    file_id: Optional[int]
    source_label: Optional[str]
    confidence: float
    page_title: Optional[str] = None
    page_type: Optional[str] = None
    # Status the compiler should use when calling create_claim.
    status: str = "needs_review"


@dataclass
class CuratorRejection:
    claim_text: str
    reason: str  # one of: "missing_quote" | "missing_chunk_id" | "quote_mismatch" | "schema"


@dataclass
class CuratorResult:
    accepted: list[CuratorAcceptedClaim] = field(default_factory=list)
    rejected: list[CuratorRejection] = field(default_factory=list)
    lint_findings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    calls: int = 0
    input_chars: int = 0

    def to_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "lint": len(self.lint_findings),
            "errors": list(self.errors),
            "calls": self.calls,
            "input_chars": self.input_chars,
        }
        if self.rejected:
            summary["rejections"] = [
                {"claim_text": r.claim_text, "reason": r.reason}
                for r in self.rejected
            ]
        return summary


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

_NORM_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Collapse whitespace + lowercase. Punctuation is kept because it
    carries meaning in code-like quotes (e.g. acronyms with periods)."""
    if not text:
        return ""
    return _NORM_RE.sub(" ", text).strip().lower()


def _quote_matches(quote: str, source_text: str, *, fuzzy_threshold: int = 92) -> bool:
    """Return True iff ``quote`` is verifiable in ``source_text``.

    Strict path: normalized substring. Falls back to rapidfuzz partial
    ratio above ``fuzzy_threshold`` (0-100). rapidfuzz is now a hard
    backend dependency (added to requirements.txt for PR C); if for
    some reason the import fails at runtime we fall back to strict
    substring only — safer to reject a borderline match than to
    silently accept it.
    """
    if not quote or not source_text:
        return False
    nq = _normalize(quote)
    ns = _normalize(source_text)
    if nq in ns:
        return True
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - defensive
        return False
    score = fuzz.partial_ratio(nq, ns)
    return score >= fuzzy_threshold


def _dedupe_key(subject: str, predicate: str, obj: str, normalized_quote: str) -> str:
    """Stable hash used to drop curator candidates that duplicate a
    deterministic claim of the same job."""
    raw = f"{subject or ''}|{predicate or ''}|{obj or ''}|{normalized_quote}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_CURATOR_SYSTEM_PROMPT = """You are a knowledge-extraction assistant.
Your job is to read a small chunk of source text and propose factual
claims, entities, and relations that an analyst could verify against
the source. You do NOT write opinions, summaries, or prose.

Output rules — STRICT:
  - Reply with a single JSON object and NOTHING ELSE. No prose. No
    Markdown fences. No commentary.
  - Every claim MUST include a "source_quote" copied verbatim from the
    source text (exact substring; whitespace may differ but words must
    match) AND a "chunk_id" exactly equal to one of the chunk IDs you
    were given.
  - If you cannot find a verbatim source_quote, do not emit the claim.
  - If you are unsure about a relationship, prefer to emit it as an
    "open_question" rather than an unverifiable "claim".

The JSON object MUST conform to this schema:
{
  "claims": [
    {
      "claim_text": str,
      "claim_type": "definition"|"role"|"procedure_step"|"configuration"|
                    "troubleshooting"|"warning"|"fact"|"open_question",
      "subject": str | null,
      "predicate": str | null,
      "object": str | null,
      "source_quote": str,        // verbatim from source_text
      "chunk_id": str,            // MUST be one of the provided chunk_ids
      "confidence": number,       // 0.0–1.0
      "page_title": str | null,
      "page_type": "entity"|"procedure"|"system"|"acronym"|"qa"|
                   "open_question"|"manual" | null
    }
  ],
  "entities": [
    {"canonical_name": str, "entity_type": str, "aliases": [str]}
  ],
  "relations": [
    {"subject": str, "predicate": str, "object": str, "source_quote": str}
  ],
  "open_questions": [
    {"question": str, "reason": str, "source_quote": str}
  ],
  "contradictions": [
    {"claim_a": str, "claim_b": str, "reason": str, "source_quote": str}
  ]
}

If a section has no items, return an empty list for that key.
"""


def build_user_prompt(
    chunks: list[CuratorChunk],
    *,
    deterministic_summary: Optional[dict] = None,
    existing_entities_brief: Optional[list[str]] = None,
) -> str:
    """Render the user message for a curator call.

    Bounds the input by ``settings.wiki_llm_curator_max_input_chars`` —
    chunks are concatenated in order until the budget runs out. The
    same budget is applied per call (not per document); when curator
    is invoked in per-chunk mode the budget cap protects each call
    individually.
    """
    budget = int(settings.wiki_llm_curator_max_input_chars or 6000)
    parts: list[str] = []
    used = 0
    chunk_block_lines: list[str] = []
    for c in chunks:
        if used >= budget:
            break
        remaining = budget - used
        snippet = c.source_text[:remaining]
        chunk_block_lines.append(
            f'### chunk_id="{c.chunk_id}"\n{snippet}'
        )
        used += len(snippet)
    parts.append("Source chunks:\n" + "\n\n".join(chunk_block_lines))

    if deterministic_summary:
        parts.append(
            "Deterministic extraction has already produced these "
            "candidates (do NOT duplicate; you may add new ones):\n"
            + json.dumps(deterministic_summary, ensure_ascii=False, indent=2)
        )
    if existing_entities_brief:
        # Bound the context to a few dozen names.
        names = existing_entities_brief[:50]
        parts.append(
            "Known wiki entities (for disambiguation only):\n"
            + ", ".join(names)
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# WikiCurator orchestrator
# ---------------------------------------------------------------------------


class WikiCurator:
    """Run the curator over bounded chunks and verify each candidate."""

    def __init__(self, client: CuratorClient, store: Optional[WikiStore] = None):
        self.client = client
        self.store = store
        self.require_quote_match = bool(
            settings.wiki_llm_curator_require_quote_match
        )
        self.require_chunk_id = bool(
            settings.wiki_llm_curator_require_chunk_id
        )
        # Mode is whether verified candidates land as 'active' or as
        # 'needs_review'. Failed candidates always become rejections /
        # lint findings, never claims.
        mode = settings.wiki_llm_curator_mode or "draft"
        self.mode = mode if mode in ("draft", "active_if_verified") else "draft"

    async def curate(
        self,
        *,
        vault_id: int,
        file_id: Optional[int],
        chunks: list[CuratorChunk],
        deterministic_summary: Optional[dict] = None,
        existing_entities_brief: Optional[list[str]] = None,
        deterministic_dedupe_keys: Optional[set[str]] = None,
    ) -> CuratorResult:
        result = CuratorResult()
        if not chunks:
            return result
        result.input_chars = sum(len(c.source_text) for c in chunks)

        # Per-chunk parallelism, bounded by the operator-facing
        # concurrency knob. Each call sees ONE chunk; the deterministic
        # summary + existing-entity brief are repeated per call so the
        # curator has the same context regardless of order.
        concurrency = max(1, min(4, int(
            getattr(settings, "wiki_llm_curator_concurrency", 1) or 1
        )))
        semaphore = asyncio.Semaphore(concurrency)

        async def _one_chunk(chunk: CuratorChunk) -> tuple[Optional[dict], Optional[str]]:
            prompt = build_user_prompt(
                [chunk],
                deterministic_summary=deterministic_summary,
                existing_entities_brief=existing_entities_brief,
            )
            async with semaphore:
                try:
                    text = await self.client.propose(
                        [
                            {"role": "system", "content": _CURATOR_SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ]
                    )
                except CuratorURLBlocked as e:
                    return None, f"ssrf_blocked: {e}"
                except Exception as e:  # broad — never propagate
                    return None, f"transport_error: {type(e).__name__}: {e}"
            parsed = extract_json(text)
            if parsed is None or not isinstance(parsed, dict):
                return None, "json_parse_failed"
            return parsed, None

        gather_results = await asyncio.gather(
            *(_one_chunk(c) for c in chunks), return_exceptions=False
        )

        # Index chunks by id for chunk_id verification + source_quote lookup.
        chunk_index: dict[str, CuratorChunk] = {c.chunk_id: c for c in chunks}
        dedupe_keys: set[str] = set(deterministic_dedupe_keys or set())
        merged_claims: list[dict] = []
        merged_contradictions: list[dict] = []

        for parsed, err in gather_results:
            result.calls += 1
            if err is not None:
                result.errors.append(err)
                continue
            if parsed is None:
                continue
            raw_claims = parsed.get("claims") or []
            if isinstance(raw_claims, list):
                merged_claims.extend(c for c in raw_claims if isinstance(c, dict))
            raw_contradictions = parsed.get("contradictions") or []
            if isinstance(raw_contradictions, list):
                merged_contradictions.extend(
                    c for c in raw_contradictions if isinstance(c, dict)
                )

        # Sort merged claims deterministically so that when concurrency > 1
        # and two chunks propose the same claim, the dedup winner is always
        # the same regardless of parallel execution order.
        merged_claims.sort(key=lambda c: (str(c.get("claim_text") or ""), str(c.get("chunk_id") or "")))

        for raw in merged_claims:
            try:
                claim_text = str(raw.get("claim_text") or "").strip()
                source_quote = str(raw.get("source_quote") or "").strip()
                chunk_id = str(raw.get("chunk_id") or "").strip()
            except (AttributeError, TypeError):
                result.rejected.append(
                    CuratorRejection(claim_text="", reason="schema")
                )
                continue
            if not claim_text:
                result.rejected.append(
                    CuratorRejection(claim_text="", reason="schema")
                )
                continue

            if not source_quote:
                result.rejected.append(
                    CuratorRejection(claim_text=claim_text, reason="missing_quote")
                )
                if self.store is not None:
                    result.lint_findings.append({
                        "finding_type": "unsupported_claim",
                        "severity": "medium",
                        "title": "Curator claim missing source_quote",
                        "details": json.dumps({
                            "subtype": "curator",
                            "claim_text": claim_text,
                        }),
                    })
                continue

            if self.require_chunk_id and chunk_id not in chunk_index:
                result.rejected.append(
                    CuratorRejection(claim_text=claim_text, reason="missing_chunk_id")
                )
                if self.store is not None:
                    result.lint_findings.append({
                        "finding_type": "unsupported_claim",
                        "severity": "medium",
                        "title": "Curator claim references unknown chunk",
                        "details": json.dumps({
                            "subtype": "curator",
                            "claim_text": claim_text,
                            "chunk_id": chunk_id,
                        }),
                    })
                continue

            chunk_for_verify = chunk_index.get(chunk_id) or (
                chunks[0] if not self.require_chunk_id else None
            )
            if chunk_for_verify is None:
                result.rejected.append(
                    CuratorRejection(claim_text=claim_text, reason="missing_chunk_id")
                )
                continue

            if self.require_quote_match and not _quote_matches(
                source_quote, chunk_for_verify.source_text
            ):
                result.rejected.append(
                    CuratorRejection(claim_text=claim_text, reason="quote_mismatch")
                )
                if self.store is not None:
                    result.lint_findings.append({
                        "finding_type": "unsupported_claim",
                        "severity": "high",
                        "title": "Curator source_quote not verifiable",
                        "details": json.dumps({
                            "subtype": "curator",
                            "claim_text": claim_text,
                            "chunk_id": chunk_id,
                            "attempted_quote": source_quote[:500],
                        }),
                    })
                continue

            subject = (raw.get("subject") or "") if isinstance(raw.get("subject"), str) else ""
            predicate = (raw.get("predicate") or "") if isinstance(raw.get("predicate"), str) else ""
            obj = (raw.get("object") or "") if isinstance(raw.get("object"), str) else ""
            key = _dedupe_key(subject, predicate, obj, _normalize(source_quote))
            if key in dedupe_keys:
                # Silent duplicate — already covered by deterministic
                # output. Don't store as rejection or lint; not a bug.
                continue
            dedupe_keys.add(key)

            confidence = raw.get("confidence")
            try:
                confidence = float(confidence) if confidence is not None else 0.5
            except (TypeError, ValueError):
                confidence = 0.5
            confidence = max(0.0, min(1.0, confidence))

            claim_type = raw.get("claim_type") or "fact"
            if not isinstance(claim_type, str):
                claim_type = "fact"

            # Status by mode: draft => always needs_review.
            # active_if_verified => active (we already passed the quote
            # check, so the verification gate is satisfied).
            status = "needs_review" if self.mode == "draft" else "active"

            page_title = raw.get("page_title") if isinstance(raw.get("page_title"), str) else None
            page_type = raw.get("page_type") if isinstance(raw.get("page_type"), str) else None

            result.accepted.append(
                CuratorAcceptedClaim(
                    claim_text=claim_text,
                    claim_type=claim_type,
                    subject=subject or None,
                    predicate=predicate or None,
                    object=obj or None,
                    source_quote=source_quote,
                    chunk_id=chunk_for_verify.chunk_id,
                    file_id=chunk_for_verify.file_id,
                    source_label=chunk_for_verify.source_label,
                    confidence=confidence,
                    page_title=page_title,
                    page_type=page_type,
                    status=status,
                )
            )

        # Contradictions become lint findings only, never claims.
        for c in merged_contradictions:
            if not isinstance(c, dict):
                continue
            result.lint_findings.append({
                "finding_type": "contradiction",
                "severity": "high",
                "title": "Curator-detected contradiction",
                "details": json.dumps({
                    "subtype": "curator",
                    "claim_a": c.get("claim_a"),
                    "claim_b": c.get("claim_b"),
                    "reason": c.get("reason"),
                    "source_quote": c.get("source_quote"),
                }),
            })

        return result


# Public utility: stable hash used by the compiler to seed
# ``deterministic_dedupe_keys`` when calling ``WikiCurator.curate``.
def deterministic_dedupe_key(
    subject: str, predicate: str, obj: str, source_quote: str
) -> str:
    return _dedupe_key(subject or "", predicate or "", obj or "", _normalize(source_quote or ""))


def verify_quote(quote: str, source_text: str, *, fuzzy_threshold: int = 92) -> bool:
    """Public alias for the source-quote verification helper.

    The wiki claim PUT route imports this when re-verifying a curator
    claim's source on transitions to status='active'. Exposing a public
    name keeps the route from depending on a private symbol.
    """
    return _quote_matches(quote, source_text, fuzzy_threshold=fuzzy_threshold)
