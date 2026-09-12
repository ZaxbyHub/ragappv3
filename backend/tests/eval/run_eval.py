"""CLI runner for the RAG eval harness (P3.5; issue #237 extensions).

Usage::

    python -m tests.eval.run_eval --golden tests/eval/fixtures/golden.jsonl \\
        --results path/to/results.jsonl --out report.json

The runner consumes:
  * ``--golden`` JSONL of expected cases (see ``eval_harness.GoldenCase``).
  * ``--results`` JSONL of per-case execution results
    (see ``eval_harness.CaseResult``). The execution layer is decoupled
    from this harness — adapters can record results from a live RAG
    query, a mocked replay, or a different system entirely.
  * ``--manifest`` optional dataset manifest (``fixtures/dataset_manifest.json``
    shape) assigning each case id to the tuning or held-out split; validated
    for disjointness before scoring and reported per split (issue #237, AC8).

Outputs:
  * Human-readable summary on stdout.
  * Machine-readable JSON at ``--out`` when provided. The report carries the
    harness summary plus a ``run_report`` envelope (run id, environment
    identity hashes, resource ids, metrics with uncertainty) loadable by
    ``app.services.eval_compare.load_run_report`` for baseline/candidate
    comparison (issue #237, AC9).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

from tests.eval.dataset_manifest import load_manifest, split_cases
from tests.eval.eval_harness import (
    CaseResult,
    EvalRunner,
    load_jsonl,
)


def _load_results(path: Path) -> List[CaseResult]:
    out: List[CaseResult] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            obj = json.loads(line)
            out.append(
                CaseResult(
                    id=str(obj["id"]),
                    retrieved_chunk_ids=list(obj.get("retrieved_chunk_ids") or []),
                    retrieved_source_labels=list(obj.get("retrieved_source_labels") or []),
                    answer=str(obj.get("answer", "")),
                    cited_source_labels=list(obj.get("cited_source_labels") or []),
                    cited_memory_labels=list(obj.get("cited_memory_labels") or []),
                    # Issue #462: restore cited_wiki_labels (was previously dropped,
                    # silently skewing wiki recall) and read the new artifact fields.
                    cited_wiki_labels=list(obj.get("cited_wiki_labels") or []),
                    invalid_citations=list(obj.get("invalid_citations") or []),
                    no_match_returned=bool(obj.get("no_match_returned", False)),
                    retrieved_artifact_ids=list(obj.get("retrieved_artifact_ids") or []),
                    cited_artifact_ids=list(obj.get("cited_artifact_ids") or []),
                    retrieved_modalities=list(obj.get("retrieved_modalities") or []),
                    retrieval_status=obj.get("retrieval_status"),
                    vision_used_artifact_ids=list(obj.get("vision_used_artifact_ids") or []),
                    vision_degraded_artifact_ids=list(
                        obj.get("vision_degraded_artifact_ids") or []
                    ),
                )
            )
    return out


def _split_summary(runner: EvalRunner, case_ids: List[str]) -> str:
    """One-line per-split summary over the runner's metrics for a split."""
    metrics = [m for m in runner.evaluate() if m.id in set(case_ids)]
    ran = sum(1 for m in metrics if m.ran)
    return f"cases={len(metrics)} ran={ran}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the RAG eval harness")
    parser.add_argument("--golden", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Dataset manifest assigning golden case ids to tuning/held-out "
        "splits (issue #237, AC8); validated for disjointness before scoring.",
    )
    args = parser.parse_args(argv)

    cases = load_jsonl(args.golden)
    splits = None
    if args.manifest is not None:
        manifest = load_manifest(args.manifest)
        splits = split_cases(cases, manifest)

    runner = EvalRunner(cases, top_k=args.top_k)
    for r in _load_results(args.results):
        runner.add_result(r)

    summary_text = runner.to_summary_text()
    print(summary_text)
    if splits is not None:
        print(
            f"tuning split:   {_split_summary(runner, [c.id for c in splits['tuning']])}"
        )
        print(
            f"held-out split: {_split_summary(runner, [c.id for c in splits['held-out']])}"
        )

    if args.out:
        summary = runner.summarize()
        report = {
            "summary": summary,
            "cases": [c.__dict__ for c in runner.evaluate()],
        }
        if splits is not None:
            report["splits"] = {
                "tuning": [c.id for c in splits["tuning"]],
                "held-out": [c.id for c in splits["held-out"]],
            }
        # Run-report envelope (issue #237, AC9): loadable by
        # app.services.eval_compare.load_run_report for baseline comparison.
        mean_keys = [
            key
            for key, value in summary.items()
            if key.endswith("_mean") or key == "no_match_correct_rate"
        ]
        report["run_report"] = {
            "run_id": "run-eval",
            "environment": _environment_stub(summary),
            "metrics": {key: summary[key] for key in mean_keys},
            "resources": sorted(c.id for c in cases),
            "intervals": {
                key: summary["uncertainty"][key]
                for key in mean_keys
                if key in summary.get("uncertainty", {})
            },
        }
        with args.out.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)
        print(f"\nReport written to {args.out}")
    return 0


def _environment_stub(summary: dict) -> dict:
    """Deterministic placeholder environment for the offline CLI report.

    The offline harness cannot know the live deployment's code/model/config;
    it stamps fixed identifiers so the report is structurally loadable and
    operators (or the live adapter) overwrite them with real fingerprints via
    ``app.services.eval_compare.environment_fingerprint`` before comparing.
    """
    from app.services.eval_compare import environment_fingerprint

    return environment_fingerprint(
        release_id="offline-harness",
        model_identity={"model": "unknown-offline"},
        config_snapshot={"top_k": summary.get("top_k")},
        resource_ids=[],
    )


if __name__ == "__main__":
    raise SystemExit(main())
