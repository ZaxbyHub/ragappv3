"""Stored baseline/candidate comparison for evaluation runs (issue #237, AC9).

Offline and deterministic: loads two run reports (each stamped with an
environment identity — code/model/config/corpus hashes — plus the resource
ids it exercised) and produces per-metric deltas with matched-resource
reporting and uncertainty intervals carried through. Identical reports
compare to a provably zero delta; a controlled regression moves exactly the
affected metric.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: Environment identity keys every RunReport.environment must carry non-empty.
ENVIRONMENT_KEYS = ("code_hash", "model_hash", "config_hash", "corpus_hash")


@dataclass
class RunReport:
    """One stored evaluation run with its environment identity.

    ``intervals`` maps a metric name to ``{"low": float, "high": float}``
    (the uncertainty interval recorded at run time, e.g. the harness
    ``uncertainty`` block). When a report carries no interval for a metric,
    the degenerate bracket ``[value, value]`` is used — an interval is always
    carried through to the comparison, never silently dropped.
    """

    run_id: str
    environment: Dict[str, str]
    metrics: Dict[str, Optional[float]]
    resources: List[str] = field(default_factory=list)
    intervals: Dict[str, Dict[str, float]] = field(default_factory=dict)


@dataclass
class MetricDelta:
    """Baseline-vs-candidate delta for one metric, intervals included."""

    metric: str
    baseline: Optional[float]
    candidate: Optional[float]
    delta: Optional[float]
    baseline_low: Optional[float] = None
    baseline_high: Optional[float] = None
    candidate_low: Optional[float] = None
    candidate_high: Optional[float] = None


@dataclass
class RunComparison:
    """Result of comparing a candidate run against a stored baseline."""

    baseline_run_id: str
    candidate_run_id: str
    deltas: Dict[str, MetricDelta] = field(default_factory=dict)
    matched_resources: List[str] = field(default_factory=list)
    baseline_environment: Dict[str, str] = field(default_factory=dict)
    candidate_environment: Dict[str, str] = field(default_factory=dict)


def load_run_report(path: str | Path) -> RunReport:
    """Load a run report JSON file into a ``RunReport``.

    Raises ``ValueError`` on missing keys, non-string environment entries,
    an environment missing any of ``ENVIRONMENT_KEYS`` with a non-empty
    value, or an ``intervals`` entry whose ``low``/``high`` bounds are
    missing or non-numeric.
    """
    p = Path(path)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"run report {p}: invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"run report {p}: expected a JSON object")
    for key in ("run_id", "environment", "metrics"):
        if key not in obj:
            raise ValueError(f"run report {p}: missing key {key!r}")
    environment = obj["environment"]
    if not isinstance(environment, dict):
        raise ValueError(f"run report {p}: environment must be an object")
    for env_key in ENVIRONMENT_KEYS:
        value = environment.get(env_key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"run report {p}: environment.{env_key} must be a non-empty string"
            )
    metrics = obj["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError(f"run report {p}: metrics must be an object")
    resources = obj.get("resources", [])
    if not isinstance(resources, list):
        raise ValueError(f"run report {p}: resources must be a list")
    raw_intervals = obj.get("intervals", {})
    if not isinstance(raw_intervals, dict):
        raise ValueError(f"run report {p}: intervals must be an object")
    intervals: Dict[str, Dict[str, float]] = {}
    for metric_name, entry in raw_intervals.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"run report {p}: intervals[{metric_name!r}] must be an object"
            )
        try:
            low = float(entry["low"])
            high = float(entry["high"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"run report {p}: intervals[{metric_name!r}] must carry numeric "
                f"'low' and 'high' bounds"
            ) from exc
        intervals[str(metric_name)] = {"low": low, "high": high}
    return RunReport(
        run_id=str(obj["run_id"]),
        environment={str(k): str(v) for k, v in environment.items()},
        metrics={
            str(k): (float(v) if v is not None else None) for k, v in metrics.items()
        },
        resources=[str(r) for r in resources],
        intervals=intervals,
    )


def environment_fingerprint(
    release_id: str,
    model_identity: Dict[str, Any],
    config_snapshot: Dict[str, Any],
    resource_ids: Sequence[str],
) -> Dict[str, str]:
    """Build a reproducible environment identity for a run report.

    ``code_hash`` derives from the release id, ``model_hash`` from a
    JSON-serialized model identity, ``config_hash`` from a JSON-serialized
    config snapshot, and ``corpus_hash`` from the sorted resource ids — all
    deterministic for identical inputs.
    """
    def _digest(payload: str) -> str:
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    return {
        "code_hash": _digest(release_id),
        "model_hash": _digest(
            json.dumps(model_identity, sort_keys=True, separators=(",", ":"))
        ),
        "config_hash": _digest(
            json.dumps(config_snapshot, sort_keys=True, separators=(",", ":"))
        ),
        "corpus_hash": _digest(
            json.dumps(sorted(resource_ids), separators=(",", ":"))
        ),
    }


def compare_runs(baseline: RunReport, candidate: RunReport) -> RunComparison:
    """Compare a candidate run against a baseline run.

    Every metric present on either side gets a ``MetricDelta``; a metric that
    is None on either side has a None delta (reported, never dropped).
    ``matched_resources`` is the sorted intersection of the two resource
    lists, so deltas are interpretable against what actually matched.
    """
    metric_names = sorted(set(baseline.metrics) | set(candidate.metrics))
    deltas: Dict[str, MetricDelta] = {}
    for name in metric_names:
        base_val = baseline.metrics.get(name)
        cand_val = candidate.metrics.get(name)

        def _bracket(report: RunReport, value: Optional[float]) -> tuple:
            # Degenerate [value, value] when the report recorded no interval —
            # uncertainty is carried through, never silently dropped.
            if value is None:
                return (None, None)
            entry = report.intervals.get(name)
            if entry is None:
                return (float(value), float(value))
            return (float(entry["low"]), float(entry["high"]))

        base_low, base_high = _bracket(baseline, base_val)
        cand_low, cand_high = _bracket(candidate, cand_val)
        if base_val is None or cand_val is None:
            delta_value: Optional[float] = None
        else:
            delta_value = cand_val - base_val
        deltas[name] = MetricDelta(
            metric=name,
            baseline=base_val,
            candidate=cand_val,
            delta=delta_value,
            baseline_low=base_low,
            baseline_high=base_high,
            candidate_low=cand_low,
            candidate_high=cand_high,
        )
    return RunComparison(
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        deltas=deltas,
        matched_resources=sorted(set(baseline.resources) & set(candidate.resources)),
        baseline_environment=dict(baseline.environment),
        candidate_environment=dict(candidate.environment),
    )


__all__ = [
    "ENVIRONMENT_KEYS",
    "MetricDelta",
    "RunComparison",
    "RunReport",
    "compare_runs",
    "environment_fingerprint",
    "load_run_report",
]
