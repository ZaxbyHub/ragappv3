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


# ---- issue #778 feedback round + #865-#868 re-arm: failing-tree pins for
# the strengthened checker legs (comment-skip, if:/continue-on-error,
# roster, zero-waiver, census, single-job literal). FULL_CI/FULL_SPEC
# satisfy every check; each mutant below flips exactly one leg and must
# go red.

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

_FULL_ROUTES = "".join(f'    path: "r{i}",{_NL}' for i in range(1, 21))

# Re-armed shape (#865-#868 closed): the spec carries NO rule-disable
# rows; the checker requires zero waivers.
FULL_SPEC = (
    "const VIEWPORTS = [{ width: 320, height: 568 }, { width: 640, height: 360 }];" + _NL
    + "const ROUTES = [" + _NL
    + _FULL_ROUTES
    + "];" + _NL
    + "axe.run();" + _NL
)


def _plant_full(tmp_path: Path, ci_text: str = FULL_CI, spec_text: str = FULL_SPEC) -> None:
    (tmp_path / CI_REL.parent).mkdir(parents=True, exist_ok=True)
    (tmp_path / CI_REL).write_text(ci_text, encoding="utf-8")
    (tmp_path / SPEC_REL.parent).mkdir(parents=True, exist_ok=True)
    (tmp_path / SPEC_REL).write_text(spec_text, encoding="utf-8")


def test_planted_full_is_green(tmp_path: Path) -> None:
    # Green baseline: the planted FULL fixture satisfies EVERY checker leg
    # (roster included — the fixture routes use the production line shape),
    # so each mutant below isolates exactly one leg.
    _plant_full(tmp_path)
    result = _run_guard(tmp_path)
    assert result.returncode == 0, result.stdout


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
    for condition in ("false", "False", '${{ !true }}', "!1"):
        neutered = FULL_CI.replace(
            "  a11y-matrix:\n",
            f"  a11y-matrix:\n    if: {condition}\n",
        )
        _plant_full(tmp_path, neutered)
        result = _run_guard(tmp_path)
        assert result.returncode == 1, condition
        assert "if:-false" in result.stdout, condition


def test_continue_on_error_job_rejected(tmp_path: Path) -> None:
    for value in ("true", '"true"', "True", "true # neutered"):
        neutered = FULL_CI.replace(
            "  a11y-matrix:\n",
            f"  a11y-matrix:\n    continue-on-error: {value}\n",
        )
        _plant_full(tmp_path, neutered)
        result = _run_guard(tmp_path)
        assert result.returncode == 1, value
        assert "continue-on-error: true" in result.stdout, value


def test_roster_below_minimum_rejected(tmp_path: Path) -> None:
    shrunken = FULL_SPEC.replace(
        _FULL_ROUTES,
        '    path: "r1",' + _NL + '    path: "r2",' + _NL,
    )
    _plant_full(tmp_path, spec_text=shrunken)
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "route roster has 2 entries" in result.stdout


def test_axe_run_options_rejected(tmp_path: Path) -> None:
    # Re-arm invariant (#865-#868 closed; carve restricted 2026-11-26): the
    # gate runs the FULL default rule set -- ANY axe.run option object
    # (rules, runOnly, exclude) is a waiver channel and is rejected. In the
    # FROZEN matrix spec any argument at all is a miss (the carve is
    # exclusive to the extensions spec); in the extensions spec it fails the
    # allowlist battery (pinned by test_axe_run_context_carve_rejects_...).
    for spelling in (
        'axe.run({ rules: { "region": { enabled: false } } });',
        'axe.run({"runOnly": ["wcag2a"]});',
        "axe.run(\n  { exclude: [[\"#app\"]] },\n);",
        'axe.run({ rules: { "region": { "enabled": false } } });',
    ):
        seeded = FULL_SPEC.replace("axe.run();", spelling)
        _plant_full(tmp_path, spec_text=seeded)
        result = _run_guard(tmp_path)
        assert result.returncode == 1, spelling
        assert "passes an axe.run argument" in result.stdout, spelling
        assert "carve is exclusive to accessibility_gate_extensions.spec.ts" in result.stdout, spelling


def test_axe_run_comment_not_counted(tmp_path: Path) -> None:
    # Comments are skipped by the option scan: prose mentioning waiver
    # spellings stays legal (the re-arm note in the spec does exactly this).
    commented = FULL_SPEC.replace(
        "axe.run();",
        "// historical: rules were once { enabled: false } via axe.run({ rules })" + _NL + "axe.run();",
    )
    _plant_full(tmp_path, spec_text=commented)
    result = _run_guard(tmp_path)
    assert "non-allowlisted argument" not in result.stdout, result.stdout


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


# ---- issue #871 AC7 context carve (RESTRICTED by the 2026-11-26 feedback
# round): the matcher admits EXACTLY the allowlisted DOM-context shapes, and
# ONLY in accessibility_gate_extensions.spec.ts — every other gated spec
# (the frozen a11y-matrix.spec.ts included) must stay argument-less.
# Rejected everywhere: bare identifiers (options smuggled via a variable),
# arbitrary calls (getOptions()), parenthesized object literals, and any
# second (top-level-comma) argument.

_EXTENSIONS_REL = Path("frontend") / "e2e" / "accessibility_gate_extensions.spec.ts"

# FULL_CI plus the extensions job (needed so the census leg accepts a
# planted extensions spec file).
FULL_CI_WITH_EXTENSIONS = FULL_CI + """  a11y-gate-extensions:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright test --reporter=line a11y-focus-outline.spec.ts accessibility_gate_extensions.spec.ts
"""


def _plant_full_with_extensions(
    tmp_path: Path,
    ext_spec_text: str,
    ci_text: str = FULL_CI_WITH_EXTENSIONS,
) -> None:
    _plant_full(tmp_path, ci_text=ci_text)
    (tmp_path / _EXTENSIONS_REL.parent).mkdir(parents=True, exist_ok=True)
    (tmp_path / _EXTENSIONS_REL).write_text(ext_spec_text, encoding="utf-8")


EXT_SPEC_BASE = (
    "const VIEWPORTS = [{ width: 320, height: 568 }, { width: 640, height: 360 }];" + _NL
    + "axe.run();" + _NL
)


def test_axe_run_context_carve_allowed_shapes(tmp_path: Path) -> None:
    # Allowed shapes live ONLY in the extensions spec now.
    for spelling in (
        "axe.run();",
        'axe.run(document.getElementById("synthetic-root"));',
        'axe.run(document.querySelector("#synthetic-root"));',
        'axe.run(document.querySelectorAll(".probe-root"));',
        'axe.run("#synthetic-root");',
        'axe.run("#synthetic-root .nested");',
    ):
        seeded = EXT_SPEC_BASE.replace("axe.run();", spelling)
        _plant_full_with_extensions(tmp_path, seeded)
        result = _run_guard(tmp_path)
        assert result.returncode == 0, (spelling, result.stdout)


def test_axe_run_context_carve_is_exclusive_to_extensions_spec(tmp_path: Path) -> None:
    # The SAME allowlisted shape planted in the FROZEN matrix spec (or any
    # other gated spec) is a miss: the carve is exclusive to the extensions
    # spec, and the matrix stays argument-less.
    seeded = FULL_SPEC.replace(
        "axe.run();",
        'axe.run(document.getElementById("synthetic-root"));',
    )
    _plant_full(tmp_path, spec_text=seeded)
    result = _run_guard(tmp_path)
    assert result.returncode == 1
    assert "carve is exclusive to accessibility_gate_extensions.spec.ts" in result.stdout


def test_axe_run_context_carve_rejects_bypass_shapes(tmp_path: Path) -> None:
    for spelling in (
        "axe.run(options);",
        "axe.run(getOptions());",
        "axe.run(({ rules: [] }));",
        "axe.run({});",
        'axe.run(document.getElementById("a"), {});',
        "axe.run(win.axeOpts);",
    ):
        seeded = EXT_SPEC_BASE.replace("axe.run();", spelling)
        _plant_full_with_extensions(tmp_path, seeded)
        result = _run_guard(tmp_path)
        assert result.returncode == 1, spelling
        assert "non-allowlisted argument" in result.stdout, spelling


def test_axe_run_context_carve_multiline_argument(tmp_path: Path) -> None:
    # The balanced-paren scan must see through line breaks inside the call.
    seeded = EXT_SPEC_BASE.replace(
        "axe.run();",
        "axe.run(\n  document.getElementById(\"synthetic-root\")\n);",
    )
    _plant_full_with_extensions(tmp_path, seeded)
    result = _run_guard(tmp_path)
    assert result.returncode == 0, result.stdout


def test_explicit_repo_root_selects_the_supplied_tree(tmp_path: Path) -> None:
    # Issue #871 AC3 (original clause), pinned in its LITERAL shape: the
    # check runs with its cwd inside an IN-REPOSITORY TEMPORARY directory
    # (created under this repo for the test, removed on exit), and
    # --repo-root must select the SUPPLIED tree instead of the competing
    # repository root the cwd walk would otherwise find. The planted tree
    # carries a violation, so a pass would prove the guard resolved some
    # OTHER tree than the one the flag supplied.
    import tempfile

    planted = tmp_path / "tree"
    _plant_full(planted)
    shrunken = FULL_SPEC.replace(_FULL_ROUTES, '    path: "r1",' + _NL)
    (planted / SPEC_REL).write_text(shrunken, encoding="utf-8")

    with tempfile.TemporaryDirectory(dir=REPO, prefix="l07-ac3-cwd-") as in_repo_temp:
        result = _run_guard_with_root(Path(in_repo_temp), planted)
    assert result.returncode == 1
    assert "route roster has 1 entries" in result.stdout
    assert "MISSING a11y-matrix" in result.stdout


def _run_guard_with_root(cwd: Path, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(A11Y_SCRIPT), "--repo-root", str(root)],
        cwd=cwd,
        timeout=120,
        capture_output=True,
        text=True,
    )


def test_repo_root_flag_requires_a_path() -> None:
    result = subprocess.run(
        [sys.executable, str(A11Y_SCRIPT), "--repo-root"],
        cwd=REPO,
        timeout=120,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--repo-root requires a path" in result.stderr


def test_allowlist_regex_is_linear_on_pathological_input() -> None:
    # Feedback round F-008/SEC-001: the pre-fix selector-segment class
    # overlapped its own separator ([ >.] vs [A-Za-z0-9_.#-]) and admitted
    # catastrophic backtracking — 4.79s EXECUTION-PROVEN at 26 dot segments.
    # The linearized grammar must settle the same input ~instantly.
    import importlib.util
    import time

    spec = importlib.util.spec_from_file_location("l07mod", A11Y_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    evil = '"#a' + ".a" * 26 + "!"
    start = time.perf_counter()
    verdict = mod._AXE_CONTEXT_ALLOWLIST.match(evil)
    elapsed = time.perf_counter() - start
    assert verdict is None
    assert elapsed < 1.0, f"allowlist match took {elapsed:.2f}s — backtracking regression"
