"""Lightweight RAG evaluation harness (P3.5).

A self-contained metrics computer that consumes a JSONL golden set and
per-query retrieval/citation outputs and emits both a structured JSON
report and a human-readable summary.

Designed to be CI-safe: the harness performs **only metric math** —
it does not call the live LLM, the live vector store, or the live
embedding model. Callers wire it up to whatever execution path they
need (mock, live, replay) and feed the resulting per-query records in.

Golden-set entry shape (JSON):

    {
        "id": "case-001",                       # required — stable case id
        "vault_id": 1,                           # optional — for record-keeping
        "query": "What did we ship last week?",  # required
        "expected_chunk_ids": ["c-1", "c-3"],    # optional — recall@k / MRR
        "expected_source_labels": ["S1", "S3"],  # optional — citation match
        "expected_facts": ["…concise…"],         # optional — fact-presence
        "expected_memories": ["M1"],             # optional — memory recall
        "expect_no_match": false                  # optional — flips correctness
    }

Per-query result shape (passed to ``EvalRunner.add_result``):

    {
        "id": "case-001",
        "retrieved_chunk_ids": ["c-1", "c-7"],
        "retrieved_source_labels": ["S1", "S2"],
        "answer": "We shipped X [S1] and Y [S2].",
        "cited_source_labels": ["S1", "S2"],
        "cited_memory_labels": [],
        "invalid_citations": [],
        "no_match_returned": false
    }

The harness intentionally does not import LanceDB / heavy services so
unit tests of the harness itself run anywhere.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


@dataclass
class GoldenCase:
    id: str
    query: str
    vault_id: Optional[int] = None
    expected_chunk_ids: List[str] = field(default_factory=list)
    expected_source_labels: List[str] = field(default_factory=list)
    expected_facts: List[str] = field(default_factory=list)
    expected_memories: List[str] = field(default_factory=list)
    expected_wiki_labels: List[str] = field(default_factory=list)
    expect_no_match: bool = False
    # Issue #462 — artifact-level expectations (independent of file/chunk ids).
    expected_artifact_ids: List[str] = field(default_factory=list)
    expected_modalities: List[str] = field(default_factory=list)
    expected_vision_mode: str = "either"  # "used" | "degraded" | "either"


@dataclass
class CaseResult:
    id: str
    retrieved_chunk_ids: List[str] = field(default_factory=list)
    retrieved_source_labels: List[str] = field(default_factory=list)
    answer: str = ""
    cited_source_labels: List[str] = field(default_factory=list)
    cited_memory_labels: List[str] = field(default_factory=list)
    cited_wiki_labels: List[str] = field(default_factory=list)
    invalid_citations: List[str] = field(default_factory=list)
    no_match_returned: bool = False
    # Issue #462 — artifact-level retrieval/citation/vision + retrieval status.
    retrieved_artifact_ids: List[str] = field(default_factory=list)
    cited_artifact_ids: List[str] = field(default_factory=list)
    # Per-source modality kinds for the retrieved artifacts (plan F: retrieve_eval
    # returns "per-source modalities"), aligned positionally with retrieved ids.
    retrieved_modalities: List[str] = field(default_factory=list)
    retrieval_status: Optional[str] = None  # "ok" | "partial" | "unavailable"
    vision_used_artifact_ids: List[str] = field(default_factory=list)
    vision_degraded_artifact_ids: List[str] = field(default_factory=list)


@dataclass
class CaseMetrics:
    """Per-case metric breakdown."""

    id: str
    recall_at_k: Optional[float]
    mrr: Optional[float]
    ndcg_at_k: Optional[float]
    citation_validity: Optional[float]
    memory_recall: Optional[float]
    wiki_recall: Optional[float]
    unsupported_citations: int
    fact_coverage: Optional[float]
    no_match_correct: Optional[bool]
    # Issue #462 — artifact metrics (computed independently of file/chunk/text).
    artifact_recall_at_k: Optional[float] = None
    artifact_mrr: Optional[float] = None
    artifact_ndcg_at_k: Optional[float] = None
    artifact_citation_validity: Optional[float] = None
    vision_use_rate: Optional[float] = None
    vision_degradation_rate: Optional[float] = None
    # Fraction of expected modalities observed among retrieved modality kinds.
    modality_match_rate: Optional[float] = None
    retrieval_status: Optional[str] = None


def load_jsonl(path: str | Path) -> List[GoldenCase]:
    """Load a JSONL golden set into typed cases. Tolerant of missing
    optional fields."""
    p = Path(path)
    cases: List[GoldenCase] = []
    with p.open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Failed to parse line {line_no} of {p}: {exc}"
                ) from exc
            cases.append(_case_from_dict(obj))
    return cases


def _case_from_dict(obj: Dict[str, Any]) -> GoldenCase:
    return GoldenCase(
        id=str(obj["id"]),
        query=str(obj["query"]),
        vault_id=obj.get("vault_id"),
        expected_chunk_ids=list(obj.get("expected_chunk_ids") or []),
        expected_source_labels=list(obj.get("expected_source_labels") or []),
        expected_facts=list(obj.get("expected_facts") or []),
        expected_memories=list(obj.get("expected_memories") or []),
        expected_wiki_labels=list(obj.get("expected_wiki_labels") or []),
        expect_no_match=bool(obj.get("expect_no_match", False)),
        expected_artifact_ids=list(obj.get("expected_artifact_ids") or []),
        expected_modalities=list(obj.get("expected_modalities") or []),
        expected_vision_mode=str(obj.get("expected_vision_mode", "either")),
    )


# Re-export the 3 metric primitives from the production module so that
# test-side callers importing from tests.eval.eval_harness continue to work.
# These take priority over any local definitions (which have been removed).
from app.services.eval_metrics import mean_reciprocal_rank as mean_reciprocal_rank
from app.services.eval_metrics import ndcg_at_k as ndcg_at_k
from app.services.eval_metrics import recall_at_k as recall_at_k


def citation_validity(
    cited: Sequence[str], available: Sequence[str]
) -> float:
    """Fraction of cited labels that reference an available source/memory.

    Returns 1.0 when no citations were emitted (vacuously valid).
    """
    if not cited:
        return 1.0
    available_set = set(available)
    valid = sum(1 for c in cited if c in available_set)
    return valid / float(len(cited))


def fact_coverage(answer: str, expected_facts: Sequence[str]) -> float:
    """Fraction of expected fact substrings that appear (case-insensitive) in the answer."""
    if not expected_facts:
        return 0.0
    a = answer.lower()
    hit = sum(1 for f in expected_facts if f.lower() in a)
    return hit / float(len(expected_facts))


# ---------- Runner ------------------------------------------------------------


class EvalRunner:
    """Aggregator that computes per-case metrics + an overall summary."""

    def __init__(self, cases: Iterable[GoldenCase], top_k: int = 5):
        self.top_k = top_k
        self._cases: Dict[str, GoldenCase] = {c.id: c for c in cases}
        self._results: Dict[str, CaseResult] = {}

    @property
    def cases(self) -> Dict[str, GoldenCase]:
        return dict(self._cases)

    def add_result(self, result: CaseResult) -> None:
        if result.id not in self._cases:
            raise KeyError(f"No golden case with id '{result.id}'")
        self._results[result.id] = result

    def evaluate(self) -> List[CaseMetrics]:
        out: List[CaseMetrics] = []
        for case_id, case in self._cases.items():
            result = self._results.get(case_id)
            if result is None:
                # Unrun cases get null metrics so the summary can flag them.
                out.append(
                    CaseMetrics(
                        id=case_id,
                        recall_at_k=None,
                        mrr=None,
                        ndcg_at_k=None,
                        citation_validity=None,
                        memory_recall=None,
                        wiki_recall=None,
                        unsupported_citations=0,
                        fact_coverage=None,
                        no_match_correct=None,
                    )
                )
                continue

            # Plan F: "denominators exclude `unavailable`". A total retrieval
            # outage must not be folded into the metric means as a 0.0 (which
            # would conflate outage with genuine zero recall). Status is still
            # recorded; only the numeric metric contributions are dropped.
            outage = result.retrieval_status == "unavailable"

            recall = (
                recall_at_k(result.retrieved_chunk_ids, case.expected_chunk_ids, self.top_k)
                if case.expected_chunk_ids and not outage
                else None
            )
            mrr = (
                mean_reciprocal_rank(result.retrieved_chunk_ids, case.expected_chunk_ids)
                if case.expected_chunk_ids and not outage
                else None
            )
            ndcg = (
                ndcg_at_k(result.retrieved_chunk_ids, case.expected_chunk_ids, self.top_k)
                if case.expected_chunk_ids and not outage
                else None
            )
            cv = (
                citation_validity(
                    result.cited_source_labels, result.retrieved_source_labels
                )
                if result.cited_source_labels or result.retrieved_source_labels
                else None
            )
            mem_recall = (
                recall_at_k(result.cited_memory_labels, case.expected_memories, self.top_k)
                if case.expected_memories
                else None
            )
            facts = (
                fact_coverage(result.answer, case.expected_facts)
                if case.expected_facts
                else None
            )
            wiki_recall = (
                recall_at_k(result.cited_wiki_labels, case.expected_wiki_labels, self.top_k)
                if case.expected_wiki_labels
                else None
            )

            no_match_correct: Optional[bool]
            if case.expect_no_match:
                no_match_correct = bool(result.no_match_returned)
            else:
                no_match_correct = (
                    None
                    if not case.expected_chunk_ids
                    else not result.no_match_returned
                )

            # Issue #462 — artifact metrics (independent of file/chunk/text metrics).
            exp_art = case.expected_artifact_ids
            artifact_recall = (
                recall_at_k(result.retrieved_artifact_ids, exp_art, self.top_k)
                if exp_art and not outage
                else None
            )
            artifact_mrr = (
                mean_reciprocal_rank(result.retrieved_artifact_ids, exp_art)
                if exp_art and not outage
                else None
            )
            artifact_ndcg = (
                ndcg_at_k(result.retrieved_artifact_ids, exp_art, self.top_k)
                if exp_art and not outage
                else None
            )
            artifact_cv = (
                citation_validity(
                    result.cited_artifact_ids, result.retrieved_artifact_ids
                )
                if (result.cited_artifact_ids or result.retrieved_artifact_ids)
                else None
            )
            vision_use = None
            vision_degraded = None
            if exp_art:
                used = set(result.vision_used_artifact_ids)
                degraded = set(result.vision_degraded_artifact_ids)
                if case.expected_vision_mode == "used":
                    vision_use = sum(1 for a in exp_art if a in used) / len(exp_art)
                    vision_degraded = sum(1 for a in exp_art if a in degraded) / len(exp_art)
                elif case.expected_vision_mode == "degraded":
                    vision_use = sum(1 for a in exp_art if a in used) / len(exp_art)
                    vision_degraded = sum(1 for a in exp_art if a in degraded) / len(exp_art)
                else:  # either — report use rate only
                    vision_use = sum(1 for a in exp_art if a in used) / len(exp_art)

            # Modality match (plan F): fraction of expected modality kinds that were
            # observed among the retrieved per-source modality kinds. Only meaningful
            # when both expectations and observations are present.
            modality_match = None
            if case.expected_modalities and not outage:
                got = set(result.retrieved_modalities)
                modality_match = (
                    sum(1 for m in case.expected_modalities if m in got) / len(case.expected_modalities)
                )

            out.append(
                CaseMetrics(
                    id=case_id,
                    recall_at_k=recall,
                    mrr=mrr,
                    ndcg_at_k=ndcg,
                    citation_validity=cv,
                    memory_recall=mem_recall,
                    wiki_recall=wiki_recall,
                    unsupported_citations=len(result.invalid_citations),
                    fact_coverage=facts,
                    no_match_correct=no_match_correct,
                    artifact_recall_at_k=artifact_recall,
                    artifact_mrr=artifact_mrr,
                    artifact_ndcg_at_k=artifact_ndcg,
                    artifact_citation_validity=artifact_cv,
                    vision_use_rate=vision_use,
                    vision_degradation_rate=vision_degraded,
                    modality_match_rate=modality_match,
                    retrieval_status=result.retrieval_status,
                )
            )
        return out

    def summarize(self, metrics: Optional[Sequence[CaseMetrics]] = None) -> Dict[str, Any]:
        m = list(metrics or self.evaluate())

        def _mean(field_name: str) -> Optional[float]:
            vals = [
                getattr(c, field_name) for c in m if getattr(c, field_name) is not None
            ]
            if not vals:
                return None
            return statistics.fmean(vals)

        no_match = [c.no_match_correct for c in m if c.no_match_correct is not None]
        no_match_rate = (
            sum(1 for x in no_match if x) / len(no_match) if no_match else None
        )
        retrieval_counts: Dict[str, int] = {}
        for c in m:
            st = c.retrieval_status
            if st:
                retrieval_counts[st] = retrieval_counts.get(st, 0) + 1
        return {
            "case_count": len(m),
            "ran_count": sum(1 for c in m if c.recall_at_k is not None or c.fact_coverage is not None or c.citation_validity is not None),
            "top_k": self.top_k,
            "recall_at_k_mean": _mean("recall_at_k"),
            "mrr_mean": _mean("mrr"),
            "ndcg_at_k_mean": _mean("ndcg_at_k"),
            "citation_validity_mean": _mean("citation_validity"),
            "memory_recall_mean": _mean("memory_recall"),
            "wiki_recall_mean": _mean("wiki_recall"),
            "fact_coverage_mean": _mean("fact_coverage"),
            "unsupported_citation_total": sum(c.unsupported_citations for c in m),
            "no_match_correct_rate": no_match_rate,
            # Issue #462 — artifact-level means + retrieval-status distribution.
            "artifact_recall_at_k_mean": _mean("artifact_recall_at_k"),
            "artifact_mrr_mean": _mean("artifact_mrr"),
            "artifact_ndcg_at_k_mean": _mean("artifact_ndcg_at_k"),
            "artifact_citation_validity_mean": _mean("artifact_citation_validity"),
            "vision_use_rate_mean": _mean("vision_use_rate"),
            "vision_degradation_rate_mean": _mean("vision_degradation_rate"),
            "modality_match_rate_mean": _mean("modality_match_rate"),
            "retrieval_status_counts": retrieval_counts,
        }

    def to_json(self, path: str | Path) -> None:
        m = self.evaluate()
        report = {
            "summary": self.summarize(m),
            "cases": [c.__dict__ for c in m],
        }
        with Path(path).open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)

    def to_summary_text(self) -> str:
        s = self.summarize()
        lines = [
            "RAG evaluation summary",
            "----------------------",
            f"Cases: {s['case_count']} (ran {s['ran_count']}, top_k={s['top_k']})",
            f"recall@k:           {_fmt(s['recall_at_k_mean'])}",
            f"MRR:                {_fmt(s['mrr_mean'])}",
            f"nDCG@k:             {_fmt(s['ndcg_at_k_mean'])}",
            f"citation validity:  {_fmt(s['citation_validity_mean'])}",
            f"memory recall:      {_fmt(s['memory_recall_mean'])}",
            f"wiki recall:        {_fmt(s['wiki_recall_mean'])}",
            f"fact coverage:      {_fmt(s['fact_coverage_mean'])}",
            f"unsupported cites:  {s['unsupported_citation_total']}",
            f"no-match correctness: {_fmt(s['no_match_correct_rate'])}",
            f"artifact recall@k:  {_fmt(s['artifact_recall_at_k_mean'])}",
            f"artifact MRR:       {_fmt(s['artifact_mrr_mean'])}",
            f"artifact nDCG@k:    {_fmt(s['artifact_ndcg_at_k_mean'])}",
            f"artifact citation validity: {_fmt(s['artifact_citation_validity_mean'])}",
            f"vision use rate:    {_fmt(s['vision_use_rate_mean'])}",
            f"vision degradation: {_fmt(s['vision_degradation_rate_mean'])}",
            f"retrieval status:   {s['retrieval_status_counts'] or 'n/a'}",
        ]
        return "\n".join(lines)


def _fmt(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.3f}"


__all__ = [
    "CaseMetrics",
    "CaseResult",
    "EvalRunner",
    "GoldenCase",
    "citation_validity",
    "fact_coverage",
    "load_jsonl",
    "mean_reciprocal_rank",
    "ndcg_at_k",
    "recall_at_k",
    "wiki_recall_mean",
]

# Convenience re-export so callers can import the function directly.
wiki_recall_mean = recall_at_k  # same algorithm, just different semantic label
