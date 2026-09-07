"""Structured answer contract helpers for RAG responses."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AnswerCitation(BaseModel):
    label: str
    evidence_type: str = "source"


class StructuredAnswer(BaseModel):
    answer: str
    citations: List[AnswerCitation] = Field(default_factory=list)
    abstained: bool = False
    # Issue #510 RAG-007 — provenance of the abstention flag:
    #   "decision"    : an explicit generation-side decision was provided
    #                   (abstention_decision was not None).
    #   "unavailable" : no decision was available (legacy prose); the flag is
    #                   NOT guessed from the answer text, so consumers must not
    #                   read abstained as meaningful in that state.
    abstention_basis: str = "unavailable"


_CITATION_RE = re.compile(r"\[(S\d+|M\d+|W\d+|K\d+)\]")


def build_answer_contract(
    content: str,
    *,
    sources: List[Dict[str, Any]],
    memories_used: List[Dict[str, Any]],
    wiki_used: List[Dict[str, Any]],
    kms_used: List[Dict[str, Any]],
    abstention_decision: Optional[bool] = None,
) -> Dict[str, Any]:
    source_labels = {s.get("source_label") for s in sources}
    memory_labels = {m.get("memory_label") for m in memories_used}
    wiki_labels = {w.get("wiki_label") for w in wiki_used}
    kms_labels = {k.get("kms_label") for k in kms_used}
    # Issue #462 — map a cited source label to its artifact modality (image/chart/
    # table/equation/code) when present, else "document". No new citation namespace;
    # label validity remains source-registry membership.
    _KNOWN_MODALITIES = frozenset(
        {"image", "chart", "table", "equation", "code"}
    )
    modality_by_label: Dict[str, str] = {
        s.get("source_label"): (s.get("modality") or "document")
        for s in sources
        if s.get("modality") in _KNOWN_MODALITIES
    }
    citations: List[AnswerCitation] = []
    for label in dict.fromkeys(_CITATION_RE.findall(content)):
        if label in source_labels:
            citations.append(
                AnswerCitation(
                    label=label,
                    evidence_type=modality_by_label.get(label, "document"),
                )
            )
        elif label in memory_labels:
            citations.append(AnswerCitation(label=label, evidence_type="memory"))
        elif label in wiki_labels:
            citations.append(AnswerCitation(label=label, evidence_type="wiki"))
        elif label in kms_labels:
            citations.append(AnswerCitation(label=label, evidence_type="kms"))
    # Issue #510 RAG-007 — abstention reflects an explicit decision from the
    # response pipeline, never a substring guess over the prose. A factual
    # answer that QUOTES "don't know" from a document is not abstaining; a
    # genuine refusal only counts when the pipeline decided it abstained.
    # Without a decision (legacy callers), the flag is False and the basis
    # marks it unavailable rather than pretending to know.
    if abstention_decision is not None:
        abstained = bool(abstention_decision)
        abstention_basis = "decision"
    else:
        abstained = False
        abstention_basis = "unavailable"
    return StructuredAnswer(
        answer=content,
        citations=citations,
        abstained=abstained,
        abstention_basis=abstention_basis,
    ).model_dump()
