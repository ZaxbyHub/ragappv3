#!/usr/bin/env python
"""Per-module mutation score gate for the nightly quality gates (issue #565).

Consumes the per-module stats files captured by the
``nightly-quality-gates`` workflow after each scoped ``mutmut run``
(``mutmut export-cicd-stats`` output when available, else the ``mutants/``
cache state), prints a ``module: killed/total (score%)`` line per module, and
exits non-zero when any module score — or the aggregate — falls below the
floor given via ``--floor``.

Fail-closed contract: if none of the known stats surfaces can be parsed for a
module, the module is reported as unparseable and the job FAILS. A gate that
cannot read its own results must never exit 0.

Usage:
    python scripts/mutation_score.py --floor 40 \
        --module app.security:stats/app.security.json ...

Each ``--module`` value is ``<label>:<path-to-stats>``. The label is used
verbatim in the report line. Exit codes: 0 pass, 1 below floor, 2 input error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _extract_counts_from_obj(obj: dict) -> tuple[int, int] | None:
    """Best-effort extraction of (killed, total) from a known-ish shape."""
    killed = survived = total = None
    for key, value in obj.items():
        lk = key.lower()
        if not isinstance(value, (int, float)):
            continue
        if lk in ("killed", "killed_count", "killedcount"):
            killed = int(value)
        elif lk in ("survived", "survived_count", "survivedcount", "surviving"):
            survived = int(value)
        elif lk in ("total", "total_mutants", "totalmutants", "mutants", "count"):
            total = int(value)
        elif lk in ("timeout", "suspicious", "skipped"):
            # counted towards total but neither killed nor survived
            total = (total or 0) + int(value)
    if killed is None:
        return None
    if total is None:
        total = killed + (survived or 0)
    return killed, total


def parse_stats(path: Path) -> tuple[int, int]:
    """Parse one stats surface, trying the documented formats.

    Supported surfaces (mutmut 3.x, first match wins):
    1. ``mutmut export-cicd-stats`` JSON — an object with killed/survived/total
       style keys (possibly nested under a top-level key).
    2. The ``mutants/`` cache state JSON — a mapping of mutant name → status
       string ("killed" / "survived" / "timeout" / ...), or an object with a
       ``mutants`` list whose entries carry ``<status>`` fields.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(raw, dict):
        direct = _extract_counts_from_obj(raw)
        if direct:
            return direct
        for value in raw.values():
            if isinstance(value, dict):
                nested = _extract_counts_from_obj(value)
                if nested:
                    return nested
            if isinstance(value, dict) and all(
                isinstance(v, (str, int, float)) for v in value.values()
            ):
                # mutant name -> status mapping
                statuses = [str(v).lower() for v in value.values()]
                total = len(statuses)
                killed = sum(1 for s in statuses if s == "killed")
                if total:
                    return killed, total
            if isinstance(value, list) and value and isinstance(value[0], dict):
                statuses = [
                    str(entry.get("status", entry.get("result", ""))).lower()
                    for entry in value
                ]
                total = len(statuses)
                killed = sum(1 for s in statuses if s == "killed")
                if total:
                    return killed, total

    if isinstance(raw, dict):
        # mutant name -> status at top level
        values = [str(v).lower() for v in raw.values() if isinstance(v, (str, int, float))]
        statuses = [v for v in values if v in ("killed", "survived", "timeout", "suspicious", "skipped", "no_gen")]
        if statuses:
            return sum(1 for s in statuses if s == "killed"), len(statuses)

    raise ValueError(f"unrecognized stats surface in {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--floor",
        type=float,
        required=True,
        help="mutation score floor (percent killed); below this the gate fails",
    )
    parser.add_argument(
        "--module",
        action="append",
        required=True,
        metavar="LABEL:PATH",
        help="module label and its stats file path",
    )
    args = parser.parse_args()

    floor = args.floor
    if not 0 <= floor <= 100:
        print(f"mutation-score: invalid floor {floor} (must be 0-100)", file=sys.stderr)
        return 2

    failures: list[str] = []
    unparseable: list[str] = []
    total_killed = 0
    total_all = 0

    for spec in args.module:
        label, sep, path_str = spec.partition(":")
        if not sep or not path_str:
            print(f"mutation-score: bad --module spec {spec!r}", file=sys.stderr)
            return 2
        path = Path(path_str)
        if not path.is_file():
            unparseable.append(f"{label}: stats file missing: {path}")
            continue
        try:
            killed, total = parse_stats(path)
        except (ValueError, json.JSONDecodeError) as exc:
            unparseable.append(f"{label}: {exc}")
            continue
        if total <= 0:
            unparseable.append(f"{label}: zero mutants reported")
            continue
        score = 100.0 * killed / total
        total_killed += killed
        total_all += total
        line = f"{label}: {killed}/{total} ({score:.1f}%)"
        print(line)
        if score < floor:
            failures.append(f"{line} below floor {floor:.1f}%")

    if total_all:
        aggregate = 100.0 * total_killed / total_all
        agg_line = f"aggregate: {total_killed}/{total_all} ({aggregate:.1f}%)"
        print(agg_line)
        if aggregate < floor:
            failures.append(f"{agg_line} below floor {floor:.1f}%")

    for item in unparseable:
        print(f"mutation-score: UNPARSEABLE {item}")

    if unparseable:
        # Fail-closed: an unreadable surface is a gate failure, never a pass.
        failures.append("unparseable stats surfaces present")
    if failures:
        print("mutation-score: FAIL")
        for item in failures:
            print(f"  {item}")
        return 1

    print("mutation-score: PASS (floor %.1f%%)" % floor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
