#!/usr/bin/env python
"""Baseline-diff gate for the nightly Schemathesis contract run (issue #565).

The KnowledgeVault API carries a large backlog of OpenAPI conformance debt
(undocumented status codes, schema violations) that predates this gate. The
nightly Schemathesis run therefore passes when every failure it finds is a
KNOWN signature recorded in the committed baseline, and FAILS when any NEW
signature appears — the same baseline-diff discipline the bandit SAST gate
(``scripts/run_bandit.py``) applies to security findings, applied to API
contract conformance.

Usage:
    python scripts/schemathesis_gate.py \
        --run-log schemathesis-run.log \
        --baseline schemathesis-baseline.json
    python scripts/schemathesis_gate.py --run-log ... --baseline ... \
        --update-baseline   # re-capture (justify the delta in the PR)

A signature is ``METHOD path :: failure title`` (phase-level failures that
are not tied to an operation use the phase name). Exit codes: 0 pass,
1 new signatures (or unparseable run), 2 input error.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

_OP_HEADER = re.compile(r"^_+ (?P<method>[A-Z]+) (?P<path>\S+) _+$")
_BULLET = re.compile(r"^- (?P<title>.+?)\s*$")
_PHASE_HEADER = re.compile(r"^_+ (?P<phase>[A-Za-z ]+ tests) _+$")


def extract_signatures(log_text: str) -> set[str]:
    """Pull ``(method path :: title)`` signatures out of a schemathesis run log.

    Failure blocks start with an operation header line; each ``- Title`` bullet
    until the next header is one signature. Bullets directly under a phase
    header (no operation) become ``<phase> :: title`` signatures.
    """
    signatures: set[str] = set()
    current_op: str | None = None
    in_failures = False
    for line in log_text.splitlines():
        if "FAILURES" in line and line.startswith("="):
            in_failures = True
            current_op = None
            continue
        if not in_failures:
            continue
        op_match = _OP_HEADER.match(line)
        if op_match:
            current_op = f"{op_match.group('method')} {op_match.group('path')}"
            continue
        phase_match = _PHASE_HEADER.match(line)
        if phase_match:
            current_op = None
            continue
        bullet = _BULLET.match(line)
        if bullet:
            title = bullet.group("title")
            signatures.add(f"{current_op or 'phase'} :: {title}")
    return signatures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-log", required=True, help="schemathesis run log")
    parser.add_argument("--baseline", required=True, help="committed baseline JSON")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="re-capture the baseline from this run instead of comparing",
    )
    args = parser.parse_args()

    log_path = Path(args.run_log)
    baseline_path = Path(args.baseline)
    if not log_path.is_file():
        print(f"schemathesis-gate: run log missing: {log_path}", file=sys.stderr)
        return 2

    log_text = log_path.read_text(encoding="utf-8", errors="replace")

    # Positive-evidence requirement: the workflow appends a SCHEMATHESIS_EXIT
    # marker after the run completes. Without it the log proves nothing — a
    # schemathesis crash before any FAILURES banner would otherwise pass the
    # gate while testing nothing (fail-closed, issue #565 / round-2 critic).
    exit_codes = re.findall(r"^SCHEMATHESIS_EXIT=(\d+)\s*$", log_text, re.MULTILINE)
    if not exit_codes:
        print(
            "schemathesis-gate: FAIL — no SCHEMATHESIS_EXIT marker in the run log "
            "(the run crashed before completing or the marker step did not run)",
            file=sys.stderr,
        )
        return 1
    run_rc = int(exit_codes[-1])
    if run_rc not in (0, 1):
        print(
            f"schemathesis-gate: FAIL — schemathesis crashed with exit code {run_rc}",
            file=sys.stderr,
        )
        return 1

    current = extract_signatures(log_text)

    if run_rc == 1 and not current:
        print(
            "schemathesis-gate: FAIL — run reported failures but no signatures could be parsed",
            file=sys.stderr,
        )
        return 1

    if args.update_baseline:
        baseline = {
            "captured": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "signature_count": len(current),
            "signatures": sorted(current),
        }
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"schemathesis-gate: baseline captured ({len(current)} signatures) -> {baseline_path}")
        return 0

    if not baseline_path.is_file():
        print(f"schemathesis-gate: baseline missing: {baseline_path}", file=sys.stderr)
        return 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    known = set(baseline.get("signatures", []))

    new = sorted(current - known)
    retired = sorted(known - current)
    print(f"schemathesis-gate: {len(current)} current signatures, {len(known)} baselined")
    for item in retired:
        print(f"  retired (no longer reproducing): {item}")
    if new:
        print("schemathesis-gate: FAIL — NEW contract violations")
        for item in new:
            print(f"  NEW: {item}")
        print(
            "\nFix the violation, or (if pre-existing conformance debt that only "
            "surfaced now) re-capture with --update-baseline and justify the delta "
            "in the PR."
        )
        return 1

    print("schemathesis-gate: PASS — no new contract violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
