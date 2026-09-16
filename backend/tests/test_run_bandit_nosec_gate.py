"""Regression tests for the SAST gate's unused-`nosec` failure path (issue #564).

Each fixture test builds an isolated bandit scan target in a temp dir (minimal
config, empty baseline), imports ``scripts/run_bandit.py`` from the repo the
suite runs in, monkeypatches its module constants onto the sandbox, and drives
``gated_scan()`` for real (bandit 1.9.x subprocess per scan).

Semantics under test:
- a ``# nosec B608`` marker that suppresses nothing must fail the gate with the
  site named (the proving behavior added by issue #564);
- a marker that suppresses a live finding must NOT fail the gate — including
  when bandit emits its per-context "nosec encountered ..., but no failed test"
  warning for the line, which the gate cross-checks with an ``--ignore-nosec``
  re-scan (the warning stream alone is false-positive-prone);
- the pre-existing new-finding baseline diff must keep failing the gate.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_BANDIT = REPO_ROOT / "scripts" / "run_bandit.py"

DEAD_MARKER_SOURCE = (
    "def q(conn, ids):\n"
    '    return conn.execute("SELECT * FROM t WHERE id IN (?)", list(ids))'
    "  # nosec B608\n"
)
# f-string with a variable table name: bandit 1.9.x fires B608 on it, so the
# marker suppresses a live finding (the cross-check must classify it alive).
LIVE_MARKER_SOURCE = (
    'TABLE = "files"\n'
    "\n"
    "\n"
    "def q(conn, x):\n"
    '    return conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (x,))'
    "  # nosec B608\n"
)
# same shape, no marker: the finding must reach the gate's baseline diff
UNMARKED_SOURCE = (
    'TABLE = "files"\n'
    "\n"
    "\n"
    "def q(conn, x):\n"
    '    return conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (x,))\n'
)


def _load_run_bandit_module():
    spec = importlib.util.spec_from_file_location("run_bandit_under_test", RUN_BANDIT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """A run_bandit module whose scan surface is a per-test sandbox dir."""
    module = _load_run_bandit_module()
    (tmp_path / "cfg.yaml").write_text(
        "exclude_dirs: []\nskips: []\n", encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "CONFIG", tmp_path / "cfg.yaml")
    return module


def _scan_surface(module, tmp_path, source: str, name: str = "fixture_target.py"):
    pkg = tmp_path / "fixture_pkg"
    pkg.mkdir(exist_ok=True)
    (pkg / name).write_text(source, encoding="utf-8", newline="\n")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"results": []}) + "\n", encoding="utf-8", newline="\n"
    )
    module.TARGETS = (str(pkg.resolve()),)
    module.BASELINE = baseline
    return module


def _run_gate(module):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = module.gated_scan()
    return code, out.getvalue() + err.getvalue()


def test_dead_marker_fails_gate(gate, tmp_path):
    """A `# nosec B608` on a line where B608 never fires must fail the gate."""
    module = _scan_surface(gate, tmp_path, DEAD_MARKER_SOURCE, "dead_marker_fixture.py")
    code, output = _run_gate(module)
    assert code != 0, f"gate passed a dead marker: {output}"
    assert "unused nosec suppression" in output
    assert "dead_marker_fixture.py" in output


def test_live_marker_passes_gate_despite_context_warning(gate, tmp_path):
    """A marker suppressing a live finding must not trip the dead-marker path.

    Bandit 1.9.x emits the per-context "nosec encountered" warning for the
    live-marker line as well; the gate must cross-check against an
    --ignore-nosec re-scan and classify the site as alive.
    """
    module = _scan_surface(gate, tmp_path, LIVE_MARKER_SOURCE, "live_marker_fixture.py")
    code, output = _run_gate(module)
    assert code == 0, f"gate failed a live suppression: {output}"


def test_unmarked_new_finding_still_fails_gate(gate, tmp_path):
    """The pre-existing baseline-diff failure path must stay intact."""
    module = _scan_surface(gate, tmp_path, UNMARKED_SOURCE, "unmarked_fixture.py")
    code, output = _run_gate(module)
    assert code != 0, f"gate passed an un-baselined finding: {output}"
    assert "new finding" in output


def test_both_failure_paths_reported_together(gate, tmp_path):
    """Dead marker + new finding in one scan: both sections, single exit 1."""
    pkg = tmp_path / "fixture_pkg"
    pkg.mkdir()
    (pkg / "dead_fixture.py").write_text(
        DEAD_MARKER_SOURCE, encoding="utf-8", newline="\n"
    )
    (pkg / "unmarked_fixture.py").write_text(
        UNMARKED_SOURCE, encoding="utf-8", newline="\n"
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"results": []}) + "\n", encoding="utf-8", newline="\n"
    )
    gate.TARGETS = (str(pkg.resolve()),)
    gate.BASELINE = baseline
    code, output = _run_gate(gate)
    assert code != 0
    assert "new finding" in output
    assert "unused nosec suppression" in output
    assert "dead_fixture.py" in output


def test_parser_dedupes_and_normalizes_paths(gate):
    """Warning parser: mixed separators, duplicate sites, non-tester noise."""
    sample = (
        "[tester]\tWARNING\tnosec encountered (B608), but no failed test on file "
        "backend/app\\services\\draft_store.py:988\n"
        "[tester]\tWARNING\tnosec encountered (B608), but no failed test on file "
        "backend/app\\services\\draft_store.py:988\n"
        "[manager]\tWARNING\tBenchmark results may be affected by concurrent "
        "processes: 40176 MiB used\n"
        "[tester]\tWARNING\tnosec encountered (B608), but no failed test on file "
        ".\\scripts/restore.py:90\n"
        "some unrelated line\n"
    )
    sites = gate._parse_dead_nosec_warnings(sample)
    assert sites == [
        ("B608", "backend/app/services/draft_store.py", 988),
        ("B608", "scripts/restore.py", 90),
    ]
    assert gate._parse_dead_nosec_warnings("") == []
    assert gate._parse_dead_nosec_warnings("no warnings here\n") == []


def test_update_baseline_warns_advisory_on_warnings(gate, tmp_path, capsys):
    """Baseline regen stays exit-0 but names nosec-usage warning sites."""
    module = _scan_surface(gate, tmp_path, DEAD_MARKER_SOURCE, "dead_marker_fixture.py")
    code = module.update_baseline()
    captured = capsys.readouterr()
    assert code == 0
    assert "nosec-usage warning site(s)" in captured.out
    assert "dead_marker_fixture.py" in captured.out
    assert module.BASELINE.exists()
