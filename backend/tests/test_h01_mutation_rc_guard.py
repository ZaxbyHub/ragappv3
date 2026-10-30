"""Issue #739 plan-promised guards: the mutmut .rc hard-fail and the real
export-shape chain through the nightly run + score steps.

Three protections (hardened by the PR #1045 feedback round):
1. A nonzero ``<module>.rc`` fails the score step even when partial (but
   parseable, above-floor) stats exist — a crashed run can never masquerade
   as a passing score.
2. An absent ``.rc`` (the frozen C2 fixture shape) keeps the step passing
   for an above-floor stats file — backward compatibility.
3. The FAITHFUL export chain: a stub ``mutmut`` mimicking mutmut 3.7 exactly
   (writes ``mutants/mutmut-cicd-stats.json``, prints only a status line to
   stdout) driven through the workflow's RUN step, proving the run step
   copies the written file into ``mutation-stats/<module>.json`` — the exact
   seam PRR-001 broke (the old step redirected the status line into the
   stats file).

NON-frozen on purpose; mirrors the frozen C2 test's step extraction
mechanics. The bash discovery rejects the Windows WSL relay (which cannot
execute the Windows interpreter the step scripts need) exactly like the
frozen sibling does.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "nightly-quality-gates.yml"
SCRIPTS = REPO / "scripts"
RUN_STEP = "Run mutmut per guard module and export per-module stats"
SCORE_STEP = "Report per-module mutation scores and enforce the survival budget"


def _bash() -> str | None:
    which = shutil.which("bash")
    if which and "system32" not in which.lower():
        # WSL's System32 relay cannot execute the Windows interpreter the
        # step scripts must run (same hazard the frozen sibling documents).
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


def _env() -> dict:
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    env["MUTATION_SCORE_FLOOR"] = "30"
    return env


def _seed_stats(backend_dir: Path, module: str, killed: int, survived: int) -> None:
    """Seed the stats file in mutmut 3.7's REAL save_cicd_stats shape:
    total already sums every bucket (so the score parser must not add the
    buckets again — PRR-016's double-count became load-bearing with the
    real shape)."""
    stats = {
        "killed": killed,
        "survived": survived,
        "total": killed + survived,
        "no_tests": 0,
        "skipped": 1,
        "suspicious": 0,
        "timeout": 0,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
    }
    (backend_dir / "mutation-stats").mkdir(parents=True, exist_ok=True)
    _write_lf(
        backend_dir / "mutation-stats" / f"{module}.json",
        json.dumps(stats, indent=4) + "\n",
    )


def test_score_step_fails_on_nonzero_mutmut_rc(tmp_path):
    """A nonzero <module>.rc must fail the score step with the named module,
    even though the stats JSON is present and above floor."""
    bash = _bash()
    if bash is None:  # pragma: no cover - CI always has bash
        pytest.skip("no usable bash available to execute the workflow step script")
    step_script = _step_run("mutation-backend", SCORE_STEP)

    module = "app.security"
    backend_dir = tmp_path / "backend"
    _write_lf(backend_dir / "mutation_modules.txt", f"{module} backend/app/security.py\n")
    # The crash leg: mutmut exited 2, yet export still wrote parseable stats
    # (100% above any floor — only the .rc can catch this masquerade).
    _seed_stats(backend_dir, module, killed=3, survived=0)
    _write_lf(backend_dir / "mutation-stats" / f"{module}.rc", "2\n")
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPTS / "mutation_score.py", tmp_path / "scripts" / "mutation_score.py")

    result = subprocess.run(
        [bash, "-c", step_script],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        env=_env(),
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
        pytest.skip("no usable bash available to execute the workflow step script")
    step_script = _step_run("mutation-backend", SCORE_STEP)

    module = "app.security"
    backend_dir = tmp_path / "backend"
    _write_lf(backend_dir / "mutation_modules.txt", f"{module} backend/app/security.py\n")
    _seed_stats(backend_dir, module, killed=2, survived=1)
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPTS / "mutation_score.py", tmp_path / "scripts" / "mutation_score.py")

    result = subprocess.run(
        [bash, "-c", step_script],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        env=_env(),
        timeout=120,
    )
    assert result.returncode == 0, (
        f"absent .rc with above-floor stats must pass; stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_run_step_copies_mutmut_written_stats_file(tmp_path):
    """The FAITHFUL export chain (PRR-001): a stub mutmut that mimics 3.7 —
    writing mutants/mutmut-cicd-stats.json and printing ONLY a status line —
    driven through the workflow's RUN step, then the score step over the
    result. Proves the run step lands parseable JSON in
    mutation-stats/<module>.json instead of the status line."""
    bash = _bash()
    if bash is None:  # pragma: no cover - CI always has bash
        pytest.skip("no usable bash available to execute the workflow step script")
    run_script = _step_run("mutation-backend", RUN_STEP)

    backend_dir = tmp_path / "backend"
    module = "app.security"
    _write_lf(backend_dir / "mutation_modules.txt", f"{module} backend/app/security.py\n")
    # The run step's heredoc rewrites pyproject.toml — it must exist.
    _write_lf(backend_dir / "pyproject.toml", "[tool.mutmut]\nsource_paths = [\"app/security.py\"]\n")
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPTS / "mutation_score.py", tmp_path / "scripts" / "mutation_score.py")

    # Stub mutmut mimicking 3.7's real behavior (the .rc capture in the run
    # step shells out to `python -m mutmut run`; this stub exits 0 and, for
    # export, writes the stats FILE + prints the status line).
    stub_dir = tmp_path / "mutmut-stub"
    (stub_dir / "mutmut").mkdir(parents=True)
    _write_lf(
        stub_dir / "mutmut" / "__init__.py",
        "__version__ = '3.7.0-stub'\n",
    )
    _write_lf(
        stub_dir / "mutmut" / "__main__.py",
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('3.7.0-stub')\n"
        "elif 'run' in sys.argv:\n"
        "    Path('mutants').mkdir(exist_ok=True)\n"
        "    sys.exit(0)\n"
        "elif 'export-cicd-stats' in sys.argv:\n"
        "    stats = {'killed': 2, 'survived': 1, 'total': 3, 'no_tests': 0,\n"
        "             'skipped': 1, 'suspicious': 0, 'timeout': 0,\n"
        "             'check_was_interrupted_by_user': 0, 'segfault': 0}\n"
        "    import json\n"
        "    Path('mutants').mkdir(exist_ok=True)\n"
        "    with open('mutants/mutmut-cicd-stats.json', 'w') as fh:\n"
        "        json.dump(stats, fh, indent=4)\n"
        "    print('Saved CI/CD stats to mutants/mutmut-cicd-stats.json')\n",
    )

    env = _env()
    env["PYTHONPATH"] = str(stub_dir) + os.pathsep + env.get("PYTHONPATH", "")
    run_result = subprocess.run(
        [bash, "-c", run_script],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert run_result.returncode == 0, (
        f"run step must succeed against the faithful stub\nstdout:\n{run_result.stdout}\n"
        f"stderr:\n{run_result.stderr}"
    )
    stats_file = backend_dir / "mutation-stats" / f"{module}.json"
    assert stats_file.is_file(), "run step must produce mutation-stats/<module>.json"
    stats = json.loads(stats_file.read_text(encoding="utf-8"))  # must BE JSON
    assert stats["killed"] == 2 and stats["total"] == 3, (
        "the copied stats must be the real stats object, not a status line"
    )
    rc_file = backend_dir / "mutation-stats" / f"{module}.rc"
    assert rc_file.read_text(encoding="utf-8").strip() == "0"

    # Then the SCORE step over the run step's output — the full chain the
    # OOB review asked for (rec #3: run step THEN score step).
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    score_script = _step_run("mutation-backend", SCORE_STEP)
    score_result = subprocess.run(
        [bash, "-c", score_script],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert score_result.returncode == 0, (
        f"score step must pass over the run step's copied stats\n"
        f"stdout:\n{score_result.stdout}\nstderr:\n{score_result.stderr}"
    )
    assert "app.security" in score_result.stdout, (
        f"score step must report the module; got: {score_result.stdout!r}"
    )
