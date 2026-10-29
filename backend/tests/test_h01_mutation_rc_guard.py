"""Issue #739 plan-promised guard: a failed mutmut run must fail the score
step even when partial (but parseable, above-floor) stats exist.

The run step records each module's mutmut exit code to
``mutation-stats/<module>.rc`` and the score step hard-fails on a nonzero
value. The frozen C2 check seeds stats JSON only (no .rc — backward
compatibility is part of its contract), so THIS test drives the nonzero-rc
leg: the exact crash path where mutmut dies mid-run yet export leaves a
parseable above-floor stats file. Without the .rc guard that crash
masquerades as a passing score.

NON-frozen on purpose; mirrors the frozen C2 test's step extraction and
seeding mechanics.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "nightly-quality-gates.yml"
SCRIPTS = REPO / "scripts"
STEP_NAME = "Report per-module mutation scores and enforce the survival budget"


def _bash() -> str | None:
    which = shutil.which("bash")
    if which:
        return which
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def _step_run(job_id: str, step_name: str) -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"][job_id]["steps"]:
        if step_name in step.get("name", ""):
            return step["run"]
    pytest.fail(f"step {step_name!r} not found in {WORKFLOW.name}")


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(text.replace("\r\n", "\n").encode("utf-8"))


def test_score_step_fails_on_nonzero_mutmut_rc(tmp_path):
    """A nonzero <module>.rc must fail the score step with the named module,
    even though the stats JSON is present and above floor."""
    bash = _bash()
    if bash is None:  # pragma: no cover - CI always has bash
        pytest.skip("no bash available to execute the workflow step script")
    step_script = _step_run("mutation-backend", STEP_NAME)

    module = "app.security"
    stats = {"killed": 3, "survived": 0, "total": 3}  # 100% — above any floor
    backend_dir = tmp_path / "backend"
    _write_lf(backend_dir / "mutation_modules.txt", f"{module} backend/app/security.py\n")
    (backend_dir / "mutation-stats").mkdir(parents=True, exist_ok=True)
    (backend_dir / "mutation-stats" / f"{module}.json").write_text(
        json.dumps(stats), encoding="utf-8"
    )
    # The crash leg: mutmut exited 2, yet export still wrote parseable stats.
    (backend_dir / "mutation-stats" / f"{module}.rc").write_text("2\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPTS / "mutation_score.py", tmp_path / "scripts" / "mutation_score.py")

    import os

    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    env["MUTATION_SCORE_FLOOR"] = "30"
    result = subprocess.run(
        [bash, "-c", step_script],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode != 0, (
        "a nonzero mutmut .rc with above-floor stats must FAIL the score step; "
        f"it exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "mutmut run failed for module" in combined and module in combined, (
        f"failure must name the module; got: {combined!r}"
    )


def test_score_step_passes_when_rc_absent(tmp_path):
    """Backward compatibility: an absent .rc (the frozen C2 fixture shape)
    must not fail the step for an above-floor stats file."""
    bash = _bash()
    if bash is None:  # pragma: no cover - CI always has bash
        pytest.skip("no bash available to execute the workflow step script")
    step_script = _step_run("mutation-backend", STEP_NAME)

    module = "app.security"
    stats = {"killed": 2, "survived": 1, "total": 3}  # 66.7% >= floor 30
    backend_dir = tmp_path / "backend"
    _write_lf(backend_dir / "mutation_modules.txt", f"{module} backend/app/security.py\n")
    (backend_dir / "mutation-stats").mkdir(parents=True, exist_ok=True)
    (backend_dir / "mutation-stats" / f"{module}.json").write_text(
        json.dumps(stats), encoding="utf-8"
    )
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPTS / "mutation_score.py", tmp_path / "scripts" / "mutation_score.py")

    import os

    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    env["MUTATION_SCORE_FLOOR"] = "30"
    result = subprocess.run(
        [bash, "-c", step_script],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"absent .rc with above-floor stats must pass; stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
