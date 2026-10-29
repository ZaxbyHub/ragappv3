"""Issue #739: pins the gate-liveness WORKFLOW wiring + the checker's
self-exclusion contract (plan-critic round 1, findings 5 and 6).

NON-frozen on purpose (the frozen interface test is
backend/tests/test_h01_gate_liveness.py).
"""

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows" / "gate-liveness.yml"
CHECKER = REPO / "scripts" / "check_gate_liveness.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_gate_liveness_wf", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scheduled_workflows():
    scheduled = []
    for wf_path in sorted((REPO / ".github/workflows").glob("*.yml")):
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        triggers = (doc.get(True) or doc.get("on") or {}) if isinstance(doc, dict) else {}
        if isinstance(triggers, dict) and triggers.get("schedule"):
            scheduled.append(wf_path.name)
    return scheduled


def test_workflow_wires_token_permissions_and_checker():
    """The workflow must authenticate gh (GH_TOKEN), stay read-only, and run
    the checker with --runs-json."""
    doc = yaml.safe_load(WF.read_text(encoding="utf-8"))
    assert doc["permissions"] == {"actions": "read", "contents": "read"}
    text = WF.read_text(encoding="utf-8")
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "check_gate_liveness.py --runs-json" in text


def test_checker_excludes_itself_and_recovers_from_stale_self():
    """Round-1 critic finding 6: a stale liveness-self history must not wedge
    the checker when every OTHER scheduled workflow is fresh."""
    mod = _load_checker()
    scheduled = _scheduled_workflows()
    assert "gate-liveness.yml" in scheduled  # itself scheduled: self-exclusion is live
    others = [name for name in scheduled if name != "gate-liveness.yml"]
    assert others, "expected at least the nightly workflows besides the checker"

    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()

    fixture = {"workflow_runs": [
        {
            "name": name,
            "path": f".github/workflows/{name}",
            "event": "schedule",
            "status": "completed",
            "conclusion": "success",
            "created_at": stale if name == "gate-liveness.yml" else fresh,
        }
        for name in scheduled
    ]}
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as fh:
        json.dump(fixture, fh)
        fixture_path = fh.name
    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--runs-json", fixture_path],
        cwd=str(REPO), capture_output=True, text=True, timeout=60,
    )
    Path(fixture_path).unlink(missing_ok=True)
    assert proc.returncode == 0, (
        "stale self-history with fresh dependencies must PASS "
        f"(self-exclusion); got rc={proc.returncode}: {proc.stdout}{proc.stderr}"
    )


def test_checker_fails_when_a_dependency_is_stale():
    """The self-exclusion must NOT exclude the nightlies: a stale nightly
    fails even when the checker's own history is fresh."""
    import tempfile

    scheduled = _scheduled_workflows()
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=1)).isoformat()
    stale = (now - timedelta(days=4)).isoformat()
    fixture = {"workflow_runs": [
        {
            "name": name,
            "path": f".github/workflows/{name}",
            "event": "schedule",
            "status": "completed",
            "conclusion": "success",
            "created_at": stale if name == "nightly.yml" else fresh,
        }
        for name in scheduled
    ]}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as fh:
        json.dump(fixture, fh)
        fixture_path = fh.name
    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--runs-json", fixture_path],
        cwd=str(REPO), capture_output=True, text=True, timeout=60,
    )
    Path(fixture_path).unlink(missing_ok=True)
    assert proc.returncode != 0
    assert "nightly.yml" in proc.stdout + proc.stderr
