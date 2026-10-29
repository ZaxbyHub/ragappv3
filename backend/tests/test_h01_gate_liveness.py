"""Issue-tracer H01 acceptance check — gate liveness (#739-AC9).

NEW-SURFACE test: it defines the interface of a not-yet-existing
``scripts/check_gate_liveness.py`` and is the frozen acceptance spec someone
else must satisfy. At the current base the file does not exist, so this test
fails with a message naming the missing module (the expected RED); once the
script lands, the test passes.

Interface contract frozen by this test
--------------------------------------
CLI:
    python scripts/check_gate_liveness.py --runs-json <fixture.json>

Fixture JSON (a subset of the ``gh api actions/runs`` shape):
    {"workflow_runs": [
        {"name": "<workflow display name>",
         "path": ".github/workflows/<file>",
         "event": "schedule",
         "status": "completed",
         "conclusion": "success",
         "created_at": "<ISO8601 with timezone offset>"}
    ]}

Behaviour: the script decides liveness from the fixture alone plus the real
workflow files under .github/workflows/ — no network, no gh CLI. A workflow
file whose ``on:`` block has a ``schedule`` trigger must have a successful
scheduled run (event=schedule, conclusion=success) in the fixture that is
less than 48h old (relative to the script's now); a workflow with no such
run is stale. Stale workflow(s) => non-zero exit with the workflow file
name(s) in the output; every scheduled workflow fresh => exit 0.

The test derives the repo's scheduled workflows from the real workflow files
(the repo's nightly.yml among them) and builds both fixtures with timestamps
computed from ``datetime.now(timezone.utc)`` so the check never rots.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
LIVENESS_SCRIPT = REPO / "scripts" / "check_gate_liveness.py"
WORKFLOWS = REPO / ".github" / "workflows"
STALENESS_LIMIT = timedelta(hours=48)


def _scheduled_workflows() -> list[Path]:
    """Workflow files under .github/workflows/ with a schedule trigger."""
    scheduled: list[Path] = []
    for wf_path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        # PyYAML parses an unquoted `on:` key as boolean True.
        triggers = (doc.get(True) or doc.get("on") or {}) if isinstance(doc, dict) else {}
        if isinstance(triggers, dict) and triggers.get("schedule"):
            scheduled.append(wf_path)
    return scheduled


def _runs_fixture(scheduled: list[Path], age: timedelta) -> dict:
    """One successful scheduled run per scheduled workflow, ``age`` old."""
    created_at = (datetime.now(timezone.utc) - age).isoformat()
    return {
        "workflow_runs": [
            {
                "name": wf_path.stem,
                "path": f".github/workflows/{wf_path.name}",
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
                "created_at": created_at,
            }
            for wf_path in scheduled
        ]
    }


def test_liveness_fails_on_stale_scheduled_workflow(tmp_path):
    """#739-AC9: check_gate_liveness flags scheduled workflows not run <48h.

    Stale leg: every scheduled workflow's last successful scheduled run is
    4 days old => the script must exit non-zero AND name the workflow file
    (at least nightly.yml, the repo's actual scheduled nightly workflow).
    Healthy leg (same test): runs 1h old => exit 0.
    """
    if not LIVENESS_SCRIPT.is_file():
        pytest.fail(
            f"scripts/check_gate_liveness.py not found: {LIVENESS_SCRIPT} — "
            "the gate-liveness CLI this acceptance check freezes does not "
            "exist yet (NEW-SURFACE RED)"
        )
    scheduled = _scheduled_workflows()
    assert scheduled, "no scheduled workflows found under .github/workflows/"
    assert any(p.name == "nightly.yml" for p in scheduled), (
        "expected the repo's nightly.yml to carry a schedule trigger"
    )

    def _run(fixture: dict, label: str) -> subprocess.CompletedProcess[str]:
        fixture_path = tmp_path / f"runs-{label}.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(LIVENESS_SCRIPT), "--runs-json", str(fixture_path)],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=120,
        )

    stale = _run(_runs_fixture(scheduled, age=4 * timedelta(days=1)), "stale")
    stale_output = stale.stdout + stale.stderr
    assert stale.returncode != 0, (
        "a scheduled workflow whose last successful scheduled run is 4 days "
        "old (>48h) must fail the liveness gate; the script exited 0"
    )
    assert "nightly.yml" in stale_output, (
        "the liveness failure output must name the stale workflow file; got: "
        f"{stale_output!r}"
    )

    healthy = _run(
        _runs_fixture(scheduled, age=timedelta(hours=1)), "healthy"
    )
    healthy_output = healthy.stdout + healthy.stderr
    assert healthy.returncode == 0, (
        "every scheduled workflow having a successful scheduled run within "
        "48h must pass; the script exited "
        f"{healthy.returncode} with output: {healthy_output!r}"
    )
