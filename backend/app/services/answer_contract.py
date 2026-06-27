"""Structured answer contract helpers for RAG responses."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from pydantic import BaseModel, Field


class AnswerCitation(BaseModel):
    label: str
    evidence_type: str = "source"


class StructuredAnswer(BaseModel):
    answer: str
    citations: List[AnswerCitation] = Field(default_factory=list)
    abstained: bool = False


_CITATION_RE = re.compile(r"\[(S\d+|M\d+|W\d+|KMS\d+)\]")


def build_answer_contract(
    content: str,
    *,
    sources: List[Dict[str, Any]],
    memories_used: List[Dict[str, Any]],
    wiki_used: List[Dict[str, Any]],
    kms_used: List[Dict[str, Any]],
) -> Dict[str, Any]:
    source_labels = {s.get("source_label") for s in sources}
    memory_labels = {m.get("memory_label") for m in memories_used}
    wiki_labels = {w.get("label_placeholder") for w in wiki_used}
    kms_labels = {k.get("label_placeholder") for k in kms_used}
    citations: List[AnswerCitation] = []
    for label in dict.fromkeys(_CITATION_RE.findall(content)):
        if label in source_labels:
            citations.append(AnswerCitation(label=label, evidence_type="document"))
        elif label in memory_labels:
            citations.append(AnswerCitation(label=label, evidence_type="memory"))
        elif label in wiki_labels:
            citations.append(AnswerCitation(label=label, evidence_type="wiki"))
        elif label in kms_labels:
            citations.append(AnswerCitation(label=label, evidence_type="kms"))
    abstained = "don't know" in content.lower() or "do not know" in content.lower()
    return StructuredAnswer(
        answer=content,
        citations=citations,
        abstained=abstained,
    ).model_dump()
