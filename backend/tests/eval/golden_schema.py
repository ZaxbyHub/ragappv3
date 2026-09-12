"""Versioned golden-dataset serialization for the offline eval harness.

Issue #237 (AC5): the golden JSONL format is versioned and round-trip
serialization is validated — dump->parse preserves every ``GoldenCase`` and
``CaseResult`` field exactly, so datasets and run reports cannot drift field
sets. Every serialized line carries ``schema_version``; ``parse_*`` reject an
unknown version loudly instead of guessing at a shape.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import List, Sequence, Type, TypeVar

from tests.eval.eval_harness import CaseResult, GoldenCase

#: Version of the golden JSONL schema emitted and accepted here. Bump on any
#: field-set change and keep ``parse_cases``/``parse_results`` per-version.
GOLDEN_SCHEMA_VERSION = "1"

_T = TypeVar("_T")


def _dump_lines(schema_version: str, records: Sequence[object]) -> str:
    lines = []
    for record in records:
        payload = _asdict(record)
        payload["schema_version"] = schema_version
        lines.append(json.dumps(payload, sort_keys=True))
    return "\n".join(lines) + ("\n" if lines else "")


def _asdict(record: object) -> dict:
    # dataclasses.asdict would deep-copy nested structures; field-wise copy is
    # enough for these flat dataclasses and keeps lists shared (read-only use).
    return {f.name: getattr(record, f.name) for f in fields(record)}  # type: ignore[arg-type]


def _parse_lines(text: str, expected_version: str, cls: Type[_T], what: str) -> List[_T]:
    out: List[_T] = []
    seen_ids: set = set()
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{what} line {line_no}: invalid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"{what} line {line_no}: expected a JSON object")
        version = obj.get("schema_version")
        if version != expected_version:
            raise ValueError(
                f"{what} line {line_no}: unsupported schema_version {version!r} "
                f"(expected {expected_version!r})"
            )
        payload = {k: v for k, v in obj.items() if k != "schema_version"}
        record_id = str(payload.get("id"))
        if record_id in seen_ids:
            raise ValueError(
                f"{what} line {line_no}: duplicate id {record_id!r}"
            )
        seen_ids.add(record_id)
        try:
            out.append(cls(**payload))  # type: ignore[call-arg]
        except TypeError as exc:
            raise ValueError(f"{what} line {line_no}: unknown field: {exc}") from exc
    return out


def dump_cases(cases: Sequence[GoldenCase]) -> str:
    """Serialize golden cases to versioned JSONL text."""
    return _dump_lines(GOLDEN_SCHEMA_VERSION, cases)


def parse_cases(text: str) -> List[GoldenCase]:
    """Parse versioned JSONL text into ``GoldenCase`` records.

    Raises ``ValueError`` on an unknown ``schema_version``, malformed JSON,
    unknown fields, or duplicate ids.
    """
    return _parse_lines(text, GOLDEN_SCHEMA_VERSION, GoldenCase, "golden cases")


def dump_results(results: Sequence[CaseResult]) -> str:
    """Serialize per-case results to versioned JSONL text."""
    return _dump_lines(GOLDEN_SCHEMA_VERSION, results)


def parse_results(text: str) -> List[CaseResult]:
    """Parse versioned results JSONL text into ``CaseResult`` records."""
    return _parse_lines(text, GOLDEN_SCHEMA_VERSION, CaseResult, "results")


def write_cases(path: str | Path, cases: Sequence[GoldenCase]) -> None:
    Path(path).write_text(dump_cases(cases), encoding="utf-8")


def write_results(path: str | Path, results: Sequence[CaseResult]) -> None:
    Path(path).write_text(dump_results(results), encoding="utf-8")


__all__ = [
    "GOLDEN_SCHEMA_VERSION",
    "dump_cases",
    "dump_results",
    "parse_cases",
    "parse_results",
    "write_cases",
    "write_results",
]
