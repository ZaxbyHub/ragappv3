"""PR #1045 feedback (OOB review rec #5, PRR-003/NEW-001/PRR-014): liveness
checker hardening pins.

- Production schedule detection must agree with a PyYAML-derived set over
  the REAL repo workflows (the frozen tests and production walked different
  parsers; this is the parity leg the out-of-band review asked for).
- Synthetic shapes: quoted ``"on":`` keys and ``.yaml`` files ARE discovered
  (both silently excluded before); comment lines are transparent.
- Failure-mode discrimination: a tier that SCHEDULED recently but failed is
  reported FAILING, not DEAD (NEW-001); a tier with no runs at all is DEAD.
- Both nightly tiers keep their schedule triggers (PRR-014: nightly.yml was
  pinned by #258 but nightly-quality-gates.yml was not).

NON-frozen on purpose.
"""

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_gate_liveness.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_gate_liveness_parity", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _yaml_scheduled() -> set[str]:
    """The YAML ground truth for which workflow files carry schedule triggers."""
    scheduled: set[str] = set()
    for wf_path in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        triggers = (doc.get(True) or doc.get("on") or {}) if isinstance(doc, dict) else {}
        if isinstance(triggers, dict) and triggers.get("schedule"):
            scheduled.add(wf_path.name)
    return scheduled


def test_production_detection_matches_yaml_ground_truth():
    """Parity leg: the text walker must find exactly the YAML-derived set
    over the real workflows."""
    mod = _load()
    detected = {p.name for p in mod._scheduled_workflows()}
    expected = _yaml_scheduled()
    assert detected == expected, (
        f"schedule detection diverged from YAML ground truth: "
        f"missing={sorted(expected - detected)} extra={sorted(detected - expected)}"
    )


def test_quoted_on_key_and_yaml_extension_discovered(tmp_path, monkeypatch):
    """PRR-003: quoted `"on":` and `.yaml` files must be discovered."""
    mod = _load()
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "quoted.yml").write_text(
        'name: Q\n"on":\n  schedule:\n    - cron: "0 3 * * *"\njobs:\n  a:\n    steps: []\n',
        encoding="utf-8",
    )
    (wf / "ext.yaml").write_text(
        'name: Y\non:\n  schedule:\n    - cron: "0 4 * * *"\njobs:\n  a:\n    steps: []\n',
        encoding="utf-8",
    )
    (wf / "plain.yml").write_text(
        'name: P\non:\n  pull_request:\njobs:\n  a:\n    steps: []\n',
        encoding="utf-8",
    )
    (wf / "commented.yml").write_text(
        'name: C\n# a comment above the trigger\non:\n'
        '# a column-0 comment INSIDE the block must not truncate the walk\n'
        '  schedule:\n    - cron: "0 5 * * *"\njobs:\n  a:\n    steps: []\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf)
    names = sorted(p.name for p in mod._scheduled_workflows())
    assert names == ["commented.yml", "ext.yaml", "quoted.yml"], names


def _run_checker(fixture: dict, monkeypatch_tmp: Path) -> subprocess.CompletedProcess:
    fixture_path = monkeypatch_tmp / "runs.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(CHECKER), "--runs-json", str(fixture_path)],
        cwd=str(REPO), capture_output=True, text=True, timeout=60,
    )


def test_failing_tier_is_named_failing_not_dead(tmp_path):
    """NEW-001: a tier that scheduled within the window but never succeeded
    is FAILING (schedule alive); a tier with no runs at all is DEAD. Both
    exit non-zero, but the output distinguishes them."""
    now = datetime.now(timezone.utc)
    fresh_fail = (now - timedelta(hours=2)).isoformat()
    fixture = {"workflow_runs": [
        {
            "name": "nq", "path": ".github/workflows/nightly-quality-gates.yml",
            "event": "schedule", "status": "completed", "conclusion": "failure",
            "created_at": fresh_fail,
        }
    ]}
    proc = _run_checker(fixture, tmp_path)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "FAILING" in combined and "nightly-quality-gates.yml" in combined
    assert "nightly.yml" in combined and "DEAD" in combined, (
        f"the tier with NO runs must be reported DEAD; got: {combined!r}"
    )


def test_both_nightly_tiers_keep_schedule_triggers():
    """PRR-014: nightly-quality-gates.yml had no schedule-presence pin (only
    nightly.yml was pinned by #258). Deleting a schedule trigger removes a
    workflow from the liveness monitor, so both tiers are pinned here."""
    for name in ("nightly.yml", "nightly-quality-gates.yml"):
        assert name in _yaml_scheduled(), (
            f"{name} lost its schedule trigger — the liveness monitor would "
            "stop covering it"
        )
