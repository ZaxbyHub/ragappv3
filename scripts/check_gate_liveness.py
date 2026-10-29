#!/usr/bin/env python3
"""Fail when a scheduled workflow has not had a successful scheduled run in 48h.

Issue #739 (ENH-03 DoD 2): the nightly quality tiers are the repo's early
warning, but a workflow that silently stops scheduling is invisible — that is
exactly how ``nightly.yml`` stayed dead for two weeks. The companion workflow
``.github/workflows/gate-liveness.yml`` runs daily, fetches the repository's
recent scheduled runs from the GitHub API into a fixture, and hands it here.

Interface (frozen by backend/tests/test_h01_gate_liveness.py):

    python scripts/check_gate_liveness.py --runs-json <fixture.json>

The fixture is a subset of the ``gh api actions/runs`` response::

    {"workflow_runs": [
        {"name": "<display name>", "path": ".github/workflows/<file>",
         "event": "schedule", "status": "completed",
         "conclusion": "success", "created_at": "<ISO8601 with tz>"}
    ]}

Semantics — decided from the fixture plus the REAL workflow files, no
network and no gh CLI:

* A workflow file under ``.github/workflows/`` whose ``on:`` block contains
  a ``schedule`` trigger is a *scheduled workflow*. Schedule detection is a
  bounded stdlib text walk (the Quality job has no YAML parser); an
  ``on:`` block too shaped for the walk to decide is treated as scheduled —
  fail-closed beats silently skipping a tier.
* This checker's own workflow file is EXCLUDED: its current invocation is
  the execution evidence, and a stale self-history must never wedge the
  checker into permanent failure after the other tiers recover.
* A scheduled workflow is healthy when the fixture holds a run with
  ``path`` == the workflow file, ``event == "schedule"`` and
  ``conclusion == "success"`` whose ``created_at`` is younger than 48h
  (relative to now). Missing, older, or only failed/cancelled runs are
  stale.
* Any stale scheduled workflow → exit 1 with one line naming it; all fresh
  → exit 0.

Stdlib-only on purpose. Run from anywhere (paths resolve from this file).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
SELF_WORKFLOW = "gate-liveness.yml"
STALENESS_LIMIT = timedelta(hours=48)

# Bare `on:` key at column 0 opens the trigger block; the next column-0 key
# closes it. Inside the block, an indented `schedule:` line is the trigger.
_ON_BLOCK_RE = re.compile(r"(?m)^on:\s*(?:#.*)?$")
_SCHEDULE_LINE_RE = re.compile(r"(?m)^\s+schedule:\s*(?:#.*)?$")
# Flow-style single-line triggers (on: {schedule: ...}) or any other shape we
# cannot decide: if the text mentions schedule near the on: line, treat as
# scheduled (fail closed).
_ON_FLOW_SCHEDULE_RE = re.compile(r"(?m)^on:.*\bschedule\b")


def _scheduled_workflows() -> list[Path]:
    """Workflow files under .github/workflows/ with a schedule trigger."""
    scheduled: list[Path] = []
    for wf_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        try:
            text = wf_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unreadable workflow: we cannot prove it is unscheduled —
            # checking it (and failing loudly on its staleness) is the
            # fail-closed direction.
            scheduled.append(wf_path)
            continue
        open_match = _ON_BLOCK_RE.search(text)
        decided = False
        if open_match is not None:
            # The block ends at the next column-0 key after the on: line.
            tail = text[open_match.end():]
            end = re.search(r"(?m)^\S", tail)
            block = tail if end is None else tail[: end.start()]
            if _SCHEDULE_LINE_RE.search(block):
                scheduled.append(wf_path)
                decided = True
            elif re.search(r"(?m)^\S", block) is None and block.strip():
                # on: block exists but holds no top-level keys we recognize
                # — undecidable; fail closed only when schedule is mentioned.
                if "schedule" in block:
                    scheduled.append(wf_path)
                    decided = True
        if not decided and _ON_FLOW_SCHEDULE_RE.search(text):
            scheduled.append(wf_path)
    return scheduled


def _parse_created_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--runs-json",
        required=True,
        help="path to a run-history fixture (gh api actions/runs subset)",
    )
    args = parser.parse_args(argv)

    fixture_path = Path(args.runs_json)
    try:
        doc = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(
            f"gate-liveness: cannot read run history from {fixture_path} "
            f"({exc.__class__.__name__}: {exc}) — refusing to pass without "
            "evidence",
            file=sys.stderr,
        )
        return 1
    runs = doc.get("workflow_runs") if isinstance(doc, dict) else None
    if not isinstance(runs, list):
        print(
            "gate-liveness: fixture has no workflow_runs list — refusing to "
            "pass without evidence",
            file=sys.stderr,
        )
        return 1

    now = datetime.now(timezone.utc)
    # Newest successful scheduled run per workflow path.
    newest_success: dict[str, datetime] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("event") != "schedule" or run.get("conclusion") != "success":
            continue
        path = run.get("path")
        created = _parse_created_at(run.get("created_at", ""))
        if not isinstance(path, str) or created is None:
            continue
        prior = newest_success.get(path)
        if prior is None or created > prior:
            newest_success[path] = created

    scheduled = [wf for wf in _scheduled_workflows() if wf.name != SELF_WORKFLOW]
    if not scheduled:
        print("gate-liveness: no scheduled workflows found under .github/workflows/")
        return 0

    stale: list[tuple[Path, datetime | None]] = []
    for wf_path in scheduled:
        rel = f".github/workflows/{wf_path.name}"
        last = newest_success.get(rel)
        if last is None or (now - last) > STALENESS_LIMIT:
            stale.append((wf_path, last))

    if stale:
        print(
            "gate-liveness: FAIL — scheduled workflow(s) with no successful "
            f"scheduled run in the last {int(STALENESS_LIMIT.total_seconds() // 3600)}h:",
            file=sys.stderr,
        )
        for wf_path, last in stale:
            if last is None:
                print(f"  {wf_path.name}: no successful scheduled run in the fixture", file=sys.stderr)
            else:
                age_h = (now - last).total_seconds() / 3600
                print(
                    f"  {wf_path.name}: last successful scheduled run "
                    f"{age_h:.1f}h ago ({last.isoformat()})",
                    file=sys.stderr,
                )
        return 1

    print(
        "gate-liveness: all scheduled workflows ran successfully within "
        f"{int(STALENESS_LIMIT.total_seconds() // 3600)}h "
        f"({len(scheduled)} checked)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
