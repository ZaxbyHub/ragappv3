"""CLI runner for the RAG eval harness (P3.5).

Usage::

    python -m tests.eval.run_eval --golden tests/eval/fixtures/golden.jsonl \\
        --results path/to/results.jsonl --out report.json

The runner consumes:
  * ``--golden`` JSONL of expected cases (see ``eval_harness.GoldenCase``).
  * ``--results`` JSONL of per-case execution results
    (see ``eval_harness.CaseResult``). The execution layer is decoupled
    from this harness — adapters can record results from a live RAG
    query, a mocked replay, or a different system entirely.

Outputs:
  * Human-readable summary on stdout.
  * Machine-readable JSON at ``--out`` when provided.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the RAG eval harness")
    parser.add_argument("--golden", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)

    cases = load_jsonl(args.golden)
    runner = EvalRunner(cases, top_k=args.top_k)
    for r in _load_results(args.results):
        runner.add_result(r)

    summary_text = runner.to_summary_text()
    print(summary_text)
    if args.out:
        runner.to_json(args.out)
        print(f"\nReport written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
