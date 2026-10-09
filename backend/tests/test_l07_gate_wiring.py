"""Issue #778 L07 CI guard tests.

``scripts/check_l07_a11y_gate_wired.py`` gates CI (Quality contracts job +
justfile) and the a11y route-x-viewport matrix runs in its own ci.yml job, so
its behavior is pinned here following the repo's gate-test precedents
(``test_l05_ci_guards.py`` subprocess runs and wiring pins): the guard passes
on the real tree, the wiring (ci.yml job + named-spec smoke step + quality
step + justfile mirror) is present, the guard fails on synthetic violating
trees, and it fails closed when the workflow is missing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
DIAGNOSTIC_LIMIT = 12_000

A11Y_SCRIPT = SCRIPTS / "check_l07_a11y_gate_wired.py"
CI_REL = Path(".github") / "workflows" / "ci.yml"
SPEC_REL = Path("frontend") / "e2e" / "a11y-matrix.spec.ts"

GOOD_CI = """\
name: CI
on: [push]
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright test --reporter=line chat-smoke.spec.ts
  a11y-matrix:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright test a11y-matrix.spec.ts
"""
# Violation 1: no job outside `e2e` references the spec.
BARE_SMOKE_CI = """\
name: CI
on: [push]
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright test --reporter=line
"""
GOOD_SPEC = "const VIEWPORTS = [{ width: 320, height: 568 }, { width: 640, height: 360 }];\n"


def _run_guard(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(A11Y_SCRIPT)],
        cwd=cwd,
        timeout=120,
        capture_output=True,
        text=True,
    )


def test_guard_passes_on_real_tree() -> None:
    result = _run_guard(REPO)
    assert result.returncode == 0, (
        result.stdout[-DIAGNOSTIC_LIMIT:] + result.stderr[-DIAGNOSTIC_LIMIT:]
    )


def test_guard_wired_into_ci_and_justfile() -> None:
    ci = (REPO / CI_REL).read_text(encoding="utf-8")
    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    assert "python scripts/check_l07_a11y_gate_wired.py" in ci, "script missing from ci.yml"
    assert "python scripts/check_l07_a11y_gate_wired.py" in justfile, "script missing from justfile"
    # The a11y matrix runs in its OWN job, distinct from the smoke job, and
    # the smoke job names its specs (no bare `npx playwright test`).
    assert "a11y-matrix:" in ci, "a11y-matrix job missing from ci.yml"
    assert "a11y-matrix.spec.ts" in ci, "spec not referenced by the a11y job"
    smoke_step = "npx playwright test --reporter=line chat-smoke.spec.ts"
    assert smoke_step in ci, "e2e smoke step does not name its specs"


def test_guard_flags_missing_wiring(tmp_path: Path) -> None:
    # Full synthetic tree with the wiring REMOVED: expect the no-wired-job
    # miss plus the bare-smoke-step miss.
    (tmp_path / CI_REL.parent).mkdir(parents=True)
    (tmp_path / CI_REL).write_text(BARE_SMOKE_CI, encoding="utf-8")
    (tmp_path / SPEC_REL.parent).mkdir(parents=True)
    (tmp_path / SPEC_REL).write_text(GOOD_SPEC, encoding="utf-8")

    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "MISSING a11y-matrix" in result.stdout
    assert "no CI job outside the 'e2e' job runs a11y-matrix.spec.ts" in result.stdout
    assert "job 'e2e' runs playwright without naming a spec file" in result.stdout


def test_guard_flags_missing_spec_file(tmp_path: Path) -> None:
    # Wiring present but the frozen spec file does not exist.
    (tmp_path / CI_REL.parent).mkdir(parents=True)
    (tmp_path / CI_REL).write_text(GOOD_CI, encoding="utf-8")

    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "MISSING a11y-matrix" in result.stdout
    assert "a11y-matrix.spec.ts not found" in result.stdout


def test_guard_flags_missing_viewport_literals(tmp_path: Path) -> None:
    # Spec exists and wiring exists, but the 640x360 (200% zoom) viewport is
    # gone — the gate would silently stop covering reflow at zoom.
    (tmp_path / CI_REL.parent).mkdir(parents=True)
    (tmp_path / CI_REL).write_text(GOOD_CI, encoding="utf-8")
    (tmp_path / SPEC_REL.parent).mkdir(parents=True)
    (tmp_path / SPEC_REL).write_text(
        "const VIEWPORTS = [{ width: 320, height: 568 }];\n", encoding="utf-8"
    )

    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "viewport literal width: 640 missing" in result.stdout
    assert "viewport literal height: 360 missing" in result.stdout


# ---- issue #778 feedback round: failing-tree pins for the strengthened
# checker legs (comment-skip, if:/continue-on-error, roster, waiver set,
# census, single-job literal). FULL_CI/FULL_SPEC satisfy every check; each
# mutant below flips exactly one leg and must go red.

FULL_CI = """name: CI
on: [push]
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright test --reporter=line chat-smoke.spec.ts
  a11y-matrix:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright test --reporter=line a11y-matrix.spec.ts
"""

_NL = chr(10)

_FULL_ROUTES = "".join(f'  {{ path: "r{i}" }},{_NL}' for i in range(1, 21))

FULL_SPEC = (
    "const VIEWPORTS = [{ width: 320, height: 568 }, { width: 640, height: 360 }];" + _NL
    + "const ROUTES = [" + _NL
    + _FULL_ROUTES
    + "];" + _NL
    + "axe.run({ rules: {" + _NL
    + '  "aria-valid-attr-value": { enabled: false },' + _NL
    + '  "nested-interactive": { enabled: false },' + _NL
    + '  "button-name": { enabled: false },' + _NL
    + '  "aria-required-children": { enabled: false },' + _NL
    + "} });" + _NL
)


def _plant_full(tmp_path: Path, ci_text: str = FULL_CI, spec_text: str = FULL_SPEC) -> None:
    (tmp_path / CI_REL.parent).mkdir(parents=True, exist_ok=True)
    (tmp_path / CI_REL).write_text(ci_text, encoding="utf-8")
    (tmp_path / SPEC_REL.parent).mkdir(parents=True, exist_ok=True)
    (tmp_path / SPEC_REL).write_text(spec_text, encoding="utf-8")


def test_comment_only_wiring_rejected(tmp_path: Path) -> None:
    # A comment naming the spec satisfies nothing (PRR-017/F-01): the run
    # step is gone, so the gate is unwired even though the literal appears.
    commented = FULL_CI.replace(
        "      - run: npx playwright test --reporter=line a11y-matrix.spec.ts",
        "      # npx playwright test --reporter=line a11y-matrix.spec.ts",
    )
    _plant_full(tmp_path, commented)
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "runs a11y-matrix.spec.ts" in result.stdout


def test_if_false_job_rejected(tmp_path: Path) -> None:
    # GitHub reports an if:-false job as success - the gate would be a no-op.
    neutered = FULL_CI.replace(
        "  a11y-matrix:\n",
        "  a11y-matrix:\n    if: false\n",
    )
    _plant_full(tmp_path, neutered)
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "if:-false" in result.stdout


def test_continue_on_error_job_rejected(tmp_path: Path) -> None:
    neutered = FULL_CI.replace(
        "  a11y-matrix:\n",
        "  a11y-matrix:\n    continue-on-error: true\n",
    )
    _plant_full(tmp_path, neutered)
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "continue-on-error: true" in result.stdout


def test_roster_below_minimum_rejected(tmp_path: Path) -> None:
    shrunken = FULL_SPEC.replace(
        _FULL_ROUTES,
        "  {" + _NL + '    path: "r1" },' + _NL + "  {" + _NL + '    path: "r2" },' + _NL,
    )
    _plant_full(tmp_path, spec_text=shrunken)
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "route roster has 2 entries" in result.stdout


def test_fifth_waiver_rejected(tmp_path: Path) -> None:
    widened = FULL_SPEC.replace(
        "} });",
        '  "region": { enabled: false },\n} });',
    )
    _plant_full(tmp_path, spec_text=widened)
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "disables 5 axe rules" in result.stdout


def test_missing_disabled_rule_rejected(tmp_path: Path) -> None:
    dropped = FULL_SPEC.replace('  "button-name": { enabled: false },\n', "")
    _plant_full(tmp_path, spec_text=dropped)
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "disabled rule 'button-name' missing" in result.stdout


def test_census_unnamed_spec_rejected(tmp_path: Path) -> None:
    # A new spec file on disk that no run line names must fail the gate
    # (F-07): otherwise it runs in no job at all.
    _plant_full(tmp_path)
    (tmp_path / SPEC_REL.parent / "orphan.spec.ts").write_text("test {}", encoding="utf-8")
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "orphan.spec.ts is not named by any playwright run line" in result.stdout


def test_second_job_literal_rejected(tmp_path: Path) -> None:
    # Re-absorbing the matrix into a second job must fail the single-job rule
    # (PRR-016): the smoke budget would silently double.
    duplicated = FULL_CI.replace(
        "      - run: npx playwright test --reporter=line chat-smoke.spec.ts",
        "      - run: npx playwright test --reporter=line chat-smoke.spec.ts a11y-matrix.spec.ts",
    )
    _plant_full(tmp_path, duplicated)
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "appears in 2 jobs" in result.stdout


def test_guard_fails_closed_without_workflow(tmp_path: Path) -> None:
    # No ci.yml at all -> exit 2, not a silent pass (the check_l05
    # missing-tree pattern).
    (tmp_path / SPEC_REL.parent).mkdir(parents=True)
    (tmp_path / SPEC_REL).write_text(GOOD_SPEC, encoding="utf-8")

    result = _run_guard(tmp_path)
    assert result.returncode == 2
