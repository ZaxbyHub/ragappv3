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
    assert "no CI job outside the 'e2e' job references" in result.stdout
    assert "bare 'npx playwright test'" in result.stdout


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
    assert "viewport literal 640 missing" in result.stdout
    assert "viewport literal 360 missing" in result.stdout


def test_guard_fails_closed_without_workflow(tmp_path: Path) -> None:
    # No ci.yml at all -> exit 2, not a silent pass (the check_l05
    # missing-tree pattern).
    (tmp_path / SPEC_REL.parent).mkdir(parents=True)
    (tmp_path / SPEC_REL).write_text(GOOD_SPEC, encoding="utf-8")

    result = _run_guard(tmp_path)
    assert result.returncode == 2
