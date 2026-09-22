"""Issue #655 regression tests — the runtime-contract gate's docs surfaces.

Pins the three contract-hygiene repairs from the 2026-09-22 frontier audit:

* D1 — ``docs/engineering/conventions.md`` carries no stale runtime pins
  (Node/Python against ALLOWED_RUNTIME; Vitest/Vite majors against the live
  ``frontend/package.json``), and re-introducing one is flagged.
* D4 — ``docs/engineering/testing.md`` and ``conventions.md`` name every ci.yml
  job and (testing.md) all six ``scripts/check_*.py`` quality contracts; removing
  one is flagged.
* D6 — ``scripts/check_secretscan.py`` states on the success path that the
  ``secretscan`` scanner itself is not run by any CI job, and its docstring
  scopes what a PASS means.

The checker logic is exercised through the same module-level pure function the
frozen acceptance drivers use (``docs_surface_failures(conventions_text,
testing_text, ci_text)``), loaded via importlib because ``scripts/`` is not an
importable package. Script-level behavior (exit codes, output text) runs via
subprocess with ``sys.executable``, following the
``backend/tests/test_issue258_build_contracts.py`` pattern. Stdlib + pytest only:
the Backend CI job has no frontend/node_modules, so nothing shells out to node.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_runtime_contract.py"
SECRETSCAN = REPO / "scripts" / "check_secretscan.py"
STALE_NODE_BULLET = (
    "- CI pins Node 20.19.0; regenerate lockfiles with the same Node release "
    "so the bundled npm version matches CI.\n"
)
MUTATED_JOB_NAME = "Docker build smoke"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_runtime_contract", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def _run_script(path: Path):
    return subprocess.run(
        [sys.executable, str(path)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _conventions_failures(module, conventions_text: str) -> list[str]:
    return module.docs_surface_failures(
        conventions_text, _doc("docs/engineering/testing.md"), _ci_text()
    )


def _testing_failures(module, testing_text: str) -> list[str]:
    return module.docs_surface_failures(
        _doc("docs/engineering/conventions.md"), testing_text, _ci_text()
    )


def _ci_text() -> str:
    return _doc(".github/workflows/ci.yml")


def _ci_job_names() -> list[str]:
    workflow = yaml.safe_load(_ci_text())
    jobs = workflow.get("jobs") or {}
    return [job.get("name") or key for key, job in jobs.items()]


# ── D1: conventions.md runtime pins ─────────────────────────────────────────


def test_conventions_md_has_no_stale_runtime_pins():
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    clean = [
        f
        for f in module.docs_surface_failures(
            real, _doc("docs/engineering/testing.md"), _ci_text()
        )
        if "conventions.md" in f
    ]
    assert not clean, f"repaired conventions.md must carry no pin violations: {clean}"

    failures = _conventions_failures(module, real + "\n" + STALE_NODE_BULLET)
    assert any(
        "conventions.md" in f and "Node" in f for f in failures
    ), f"stale Node pin re-introduction must be flagged with the doc and the pin in one message: {failures}"


def test_conventions_md_natural_language_pin_forms_flagged():
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    for stale in (
        "- CI pins Node.js 20.19.0 for lockfile regeneration.\n",
        "- Any Node 20.x release works for lockfiles.\n",
        "- Lockfiles need Vitest 4.x and Vite >= 6.\n",
    ):
        failures = _conventions_failures(module, real + "\n" + stale)
        assert any("conventions.md" in f for f in failures), (
            f"natural-language pin form must be flagged: {stale!r} -> {failures}"
        )


def test_conventions_md_lists_every_ci_job():
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    missing = [name for name in _ci_job_names() if name not in real]
    assert not missing, f"conventions.md job inventory is missing ci.yml jobs: {missing}"
    trimmed = real.replace(MUTATED_JOB_NAME, "[removed-job]")
    assert trimmed != real, "sanity: the mutated job name must exist in the real doc"
    failures = _conventions_failures(module, trimmed)
    assert any(
        "conventions.md" in f and MUTATED_JOB_NAME in f for f in failures
    ), f"removing a job name from conventions.md must be flagged: {failures}"


# ── D4: testing.md CI gate inventory ────────────────────────────────────────


def test_testing_md_lists_every_ci_job():
    module = _load_checker()
    real = _doc("docs/engineering/testing.md")
    clean = [
        f
        for f in module.docs_surface_failures(
            _doc("docs/engineering/conventions.md"), real, _ci_text()
        )
        if "testing.md" in f
    ]
    assert not clean, f"repaired testing.md must carry no inventory violations: {clean}"

    assert trimmed_failures_contains_job(module, real), (
        "removing a job name from testing.md must be flagged with the doc and "
        "the missing job in one message"
    )


def trimmed_failures_contains_job(module, real: str) -> bool:
    trimmed = real.replace(MUTATED_JOB_NAME, "[removed-job]")
    failures = _testing_failures(module, trimmed)
    return any(
        "testing.md" in f and MUTATED_JOB_NAME in f for f in failures
    )


# ── D6: the secretscan gate's honest labeling ───────────────────────────────


def test_secretscan_summary_states_scanner_not_wired():
    result = _run_script(SECRETSCAN)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"check_secretscan must still pass: {combined}"
    assert (
        "not run by any CI job" in combined
    ), f"success output must disclose the scanner is not wired: {combined}"


def test_secretscan_docstring_states_gate_scope():
    source = " ".join(SECRETSCAN.read_text(encoding="utf-8").split())
    assert "not that a scan ran" in source, (
        "the docstring must scope what a PASS means until the scanner is wired"
    )


# ── Guard robustness (fail-loud, never crash) ───────────────────────────────


_WIRING_CALL = "    check_docs_surfaces(failures)\n"


def _copied_checker_tree(destination: Path) -> None:
    """Mirror every repo file the checker reads into a temp root.

    ``check_runtime_contract.py`` resolves ``ROOT`` from its own location
    (``parents[1]``), so a mutated copy of the script only behaves like the
    repo gate when the surfaces it reads exist under the same relative paths.
    """
    for relative in (
        "docs/engineering/conventions.md",
        "docs/engineering/testing.md",
        ".github/workflows/ci.yml",
        "frontend/package.json",
        "CONTRIBUTING.md",
        ".devcontainer/devcontainer.json",
        "Dockerfile",
        "frontend/Dockerfile",
    ):
        source = REPO / relative
        if source.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())


def test_docs_surface_wired_into_main():
    """Integration pin (Phase 4.5 review): the docs surface must stay wired.

    Runs the REAL script from a mirrored temp root with a stale pin injected
    into conventions.md, twice: with the original source (the gate must exit
    non-zero and name conventions.md) and with the ``check_docs_surfaces``
    call removed from ``main()`` (the gate must go blind and exit 0). Proves
    the ``main()`` wiring is load-bearing — deleting the call re-opens D1.
    """
    source = CHECKER.read_text(encoding="utf-8")
    assert _WIRING_CALL in source, (
        "the check_docs_surfaces call disappeared from main() — this test "
        "pins the wiring itself; restore it or update this pin deliberately"
    )

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        _copied_checker_tree(root)
        conventions = root / "docs" / "engineering" / "conventions.md"
        conventions.write_text(
            conventions.read_text(encoding="utf-8") + "\n" + STALE_NODE_BULLET,
            encoding="utf-8",
        )
        script_dir = root / "scripts"
        script_dir.mkdir()

        wired = script_dir / "check_runtime_contract.py"
        wired.write_text(source, encoding="utf-8")
        wired_result = subprocess.run(
            [sys.executable, str(wired)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert wired_result.returncode != 0, (
            "with the wiring present, a stale pin must fail the real gate: "
            f"stdout={wired_result.stdout!r} stderr={wired_result.stderr!r}"
        )
        assert "conventions.md" in wired_result.stderr

        unwired = script_dir / "check_runtime_contract.py"
        unwired.write_text(source.replace(_WIRING_CALL, ""), encoding="utf-8")
        unwired_result = subprocess.run(
            [sys.executable, str(unwired)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert unwired_result.returncode == 0, (
            "with the wiring removed the gate must be blind again (this is "
            "the regression the pin exists to catch): "
            f"stderr={unwired_result.stderr!r}"
        )


def test_docs_surfaces_missing_file_reports_failure():
    module = _load_checker()
    with tempfile.TemporaryDirectory() as scratch:
        original_root = module.ROOT
        module.ROOT = Path(scratch)
        try:
            failures: list[str] = []
            module.check_docs_surfaces(failures)
        finally:
            module.ROOT = original_root
    assert failures, "missing docs surfaces must produce failures"
    assert all("missing" in f for f in failures), (
        f"missing-file failures must be clean missing-surface messages: {failures}"
    )


def test_dep_major_tracks_package_json():
    module = _load_checker()
    package_text = _doc("frontend/package.json")
    assert module._dep_major(package_text, "vitest", []) is not None
    assert module._dep_major(package_text, "vite", []) is not None
    # The helper derives from the live file: a synthetic bump moves the major.
    assert module._dep_major('{"vitest": "~9.0.0"}', "vitest", []) == "9"
    assert module._dep_major('{"vite": "^7.1.2"}', "vite", []) == "7"
    probe: list[str] = []
    assert module._dep_major("{}", "vitest", probe) is None
    assert probe and "frontend/package.json" in probe[0], (
        "a missing dependency must fail loud with a runtime-contract message"
    )


# ── ci.yml job-name parser (final-critic hardening) ─────────────────────────


def test_ci_job_display_names_parser():
    module = _load_checker()
    real = module._ci_job_display_names(_ci_text())
    assert real == [
        "Frontend",
        "Playwright e2e smoke",
        "Quality contracts",
        "Detect docker scope",
        "Docker build smoke",
        "SAST (bandit)",
        "Backend",
    ], f"real ci.yml ground truth changed or the parser regressed: {real}"

    # Quoted scalar values must parse without their quote characters.
    quoted = (
        "name: CI\n"
        "jobs:\n"
        '  frontend:\n    name: "Frontend"\n'
        "  backend:\n    name: 'Backend'\n"
    )
    assert module._ci_job_display_names(quoted) == ["Frontend", "Backend"]

    # Indentation inside jobs: may differ from 4 spaces; every job is found.
    deep = (
        "jobs:\n"
        "      frontend:\n"
        "        name: Frontend\n"
        "      backend:\n"
        "        name: Backend\n"
    )
    assert module._ci_job_display_names(deep) == ["Frontend", "Backend"]

    # A job without a name: falls back to its key.
    unnamed = "jobs:\n  frontend:\n    runs-on: ubuntu-latest\n  backend:\n    name: Backend\n"
    assert module._ci_job_display_names(unnamed) == ["frontend", "Backend"]

    # A one-of-seven omission can never silently truncate the inventory: with
    # the name: gone the parser falls back to the job key, so the job stays in
    # the ground truth (as 'docker-smoke') and the gate flags the doc naming it.
    mutated = _ci_text().replace("    name: Docker build smoke\n", "    runs-on: ubuntu-latest\n")
    names = module._ci_job_display_names(mutated)
    assert "docker-smoke" in names and "Docker build smoke" not in names and len(names) == 7, (
        f"a name:-less job must fall back to its key so the inventory stays "
        f"complete: {names}"
    )

    # Absent or empty jobs: must fail loud ([]) rather than []-by-drift being
    # indistinguishable — the caller turns [] into a runtime-contract failure.
    assert module._ci_job_display_names("name: CI\n") == []


def test_docs_surface_flags_job_renamed_in_ci():
    """Inventory drift is caught in BOTH directions: if ci.yml renames a job,
    docs still naming the old display name must be flagged — the parser's
    ground truth tracks the workflow, not the docs.
    """
    module = _load_checker()
    ci_mutated = _ci_text().replace(
        "    name: Docker build smoke\n", "    name: Docker build check\n"
    )
    assert "Docker build check" in module._ci_job_display_names(ci_mutated), (
        "sanity: the parser must see the renamed job"
    )
    failures = module.docs_surface_failures(
        _doc("docs/engineering/conventions.md"),
        _doc("docs/engineering/testing.md"),
        ci_mutated,
    )
    assert any(
        "testing.md" in f and "Docker build check" in f for f in failures
    ), f"renaming a ci.yml job must flag the missing new name in the docs: {failures}"
    assert any(
        "conventions.md" in f and "Docker build check" in f for f in failures
    ), f"renaming a ci.yml job must flag both doc inventories: {failures}"


def test_docs_surface_fails_loud_on_unparseable_jobs():
    module = _load_checker()
    failures = module.docs_surface_failures(
        _doc("docs/engineering/conventions.md"),
        _doc("docs/engineering/testing.md"),
        "name: CI\non: push\n",
    )
    assert any("no job names parsed" in f for f in failures), (
        f"an unparseable jobs: mapping must fail loud: {failures}"
    )
