"""Tuning/held-out dataset manifests for the offline eval harness.

Issue #237 (AC8): experiments must tune on one split and report on a
disjoint held-out split. A manifest assigns every golden case id to exactly
one split; ``validate_manifest`` enforces disjointness, non-emptiness and
that every manifest id exists in the golden set — split discipline is
enforced, never assumed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence

from tests.eval.eval_harness import GoldenCase

MANIFEST_SCHEMA_VERSION = "1"


@dataclass
class DatasetManifest:
    """Assignment of golden case ids to tuning and held-out splits."""

    schema_version: str
    tuning_case_ids: List[str] = field(default_factory=list)
    held_out_case_ids: List[str] = field(default_factory=list)


def load_manifest(path: str | Path) -> DatasetManifest:
    """Load a dataset manifest JSON file.

    Raises ``ValueError`` on missing keys or a wrong ``schema_version``.
    """
    p = Path(path)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"dataset manifest {p}: invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"dataset manifest {p}: expected a JSON object")
    version = obj.get("schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"dataset manifest {p}: unsupported schema_version {version!r} "
            f"(expected {MANIFEST_SCHEMA_VERSION!r})"
        )
    for key in ("tuning_case_ids", "held_out_case_ids"):
        if not isinstance(obj.get(key), list):
            raise ValueError(f"dataset manifest {p}: missing list key {key!r}")
    return DatasetManifest(
        schema_version=str(version),
        tuning_case_ids=[str(x) for x in obj["tuning_case_ids"]],
        held_out_case_ids=[str(x) for x in obj["held_out_case_ids"]],
    )


def validate_manifest(manifest: DatasetManifest, cases: Sequence[GoldenCase]) -> None:
    """Validate a manifest against a golden case set.

    Raises ``ValueError`` when the splits overlap, when a manifest id is
    unknown to the golden set, or when either split is empty.
    """
    known = {case.id for case in cases}
    tuning = set(manifest.tuning_case_ids)
    held_out = set(manifest.held_out_case_ids)
    if tuning & held_out:
        overlap = sorted(tuning & held_out)
        raise ValueError(
            f"dataset manifest splits overlap on case ids {overlap} — "
            "tuning and held-out must be disjoint"
        )
    if not tuning:
        raise ValueError("dataset manifest tuning split is empty")
    if not held_out:
        raise ValueError("dataset manifest held-out split is empty")
    for split_name, split_ids in (("tuning", tuning), ("held-out", held_out)):
        unknown = sorted(split_ids - known)
        if unknown:
            raise ValueError(
                f"dataset manifest {split_name} split references unknown case "
                f"ids {unknown}"
            )


def split_cases(
    cases: Sequence[GoldenCase], manifest: DatasetManifest
) -> Dict[str, List[GoldenCase]]:
    """Split golden cases into ``{"tuning": [...], "held-out": [...]}``.

    Validates first; every case appears in exactly one split.
    """
    validate_manifest(manifest, cases)
    tuning = set(manifest.tuning_case_ids)
    return {
        "tuning": [c for c in cases if c.id in tuning],
        "held-out": [c for c in cases if c.id not in tuning],
    }


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "DatasetManifest",
    "load_manifest",
    "split_cases",
    "validate_manifest",
]
