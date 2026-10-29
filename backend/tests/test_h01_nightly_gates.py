"""Issue-tracer H01 acceptance checks — nightly gate workflows (#739).

Frozen acceptance spec for the nightly gate defects traced by issue #739.
Each test maps to exactly one AC and cites it in its docstring. The tests
execute the REAL workflow step scripts (extracted from the committed YAML)
rather than paraphrases of them, so a fix must repair the actual step text:

* AC1 — every workflow step uses only GitHub Actions schema keys.
* AC2 — the mutation-score report step passes per-module args intact.
* AC3 — the mutmut step rewrites ``source_paths`` so they resolve from the
  job's working directory (``backend/``).
* AC4 — the stryker-frontend job has timeout headroom (>= 180 min) or a
  sharding matrix.
* AC5 — the schemathesis step records the tool's real exit code in the
  ``SCHEMATHESIS_EXIT`` log marker even under ``-eo pipefail``.
* AC7 — the runtime-contract gate checks EVERY workflow file's version pins,
  not just ci.yml.
* AC8 — the runtime-contract gate rejects a stale/placeholder digest pin.

Hermetic: no network, no gh CLI, all temp state under pytest ``tmp_path``,
and the repository working tree is never modified.
"""

import contextlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
SCRIPTS = REPO / "scripts"

# The GitHub Actions step schema: keys a step mapping may legally carry.
STEP_SCHEMA = {
    "id",
    "if",
    "name",
    "uses",
    "run",
    "shell",
    "with",
    "env",
    "continue-on-error",
    "timeout-minutes",
    "working-directory",
}

# Unique suffix counter so repeated spec_from_file_location loads never
# collide in sys.modules (scripts/ has no __init__.py).
_SPEC_NAMES = itertools.count()


@contextlib.contextmanager
def _no_bytecode():
    """Keep spec_from_file_location loads from writing scripts/__pycache__."""
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = old


def _load_script(filename: str):
    """Import scripts/<filename> as a fresh module (no repo-tree writes)."""
    path = SCRIPTS / filename
    if not path.is_file():
        pytest.fail(f"scripts/{filename} not found: {path}")
    name = f"_h01_acceptance_{Path(filename).stem}_{next(_SPEC_NAMES)}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with _no_bytecode():
        spec.loader.exec_module(module)
    return module


def _find_bash() -> str | None:
    """Locate a bash that can run Windows interpreters.

    On CI (ubuntu) any bash works. On Windows, ``shutil.which("bash")``
    usually resolves to the WSL relay (C:\\Windows\\System32\\bash.exe),
    which cannot execute the Windows ``python`` the step scripts must run —
    so the Git-for-Windows install is preferred there.
    """
    if os.name != "nt":
        return shutil.which("bash")
    candidates = [
        os.environ.get("GIT_BASH_PATH"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


BASH = _find_bash()
requires_bash = pytest.mark.skipif(
    BASH is None, reason="no bash available to execute the workflow step scripts"
)


def _nightly_gates_workflow() -> dict:
    return yaml.safe_load(
        (WORKFLOWS / "nightly-quality-gates.yml").read_text(encoding="utf-8")
    )


def _step_run(job_id: str, step_name: str) -> str:
    """Extract a step's ``run:`` script from nightly-quality-gates.yml."""
    workflow = _nightly_gates_workflow()
    for step in workflow["jobs"][job_id].get("steps", []):
        if step.get("name") == step_name:
            return step["run"]
    pytest.fail(f"step {step_name!r} not found in job {job_id!r}")


def _write_lf(path: Path, text: str) -> None:
    """Write text with LF endings so bash ``read`` never sees stray CRs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _bash_env() -> dict[str, str]:
    """Env for executing workflow steps: real interpreter first on PATH."""
    env = dict(os.environ)
    interpreter_dir = os.path.dirname(sys.executable)
    env["PATH"] = interpreter_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = ""  # hermetic: drop any outer interpreter state
    return env


def test_workflow_steps_use_only_schema_keys():
    """#739-AC1: every step in every workflow uses only Actions schema keys.

    A key dedented out of ``with:`` to step level (like nightly.yml's
    ``if-no-files-found``) is silently ignored by Actions — the setting stops
    applying and no error is raised. The frozen contract: the sorted list of
    "<file>: <key>" offenders over all .github/workflows/*.yml is empty.
    """
    offenders: list[str] = []
    for wf_path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                for key in sorted(set(step) - STEP_SCHEMA):
                    offenders.append(f"{wf_path.name}: {key}")
    assert sorted(offenders) == [], (
        "workflow steps carry keys outside the GitHub Actions step schema "
        "(they are silently ignored by Actions): "
        f"{sorted(offenders)}"
    )


def test_mutation_score_step_accepts_module_path_lines(tmp_path):
    """#739-AC2: the mutation-score report step must handle full scope lines.

    mutation_modules.txt lines are ``<module> <path>`` pairs (the format the
    "Resolve mutation scope" step writes). The step reads each line and must
    pass ``--module <module>:mutation-stats/<module>.json`` to
    scripts/mutation_score.py without letting the path half leak into argv.
    The step script is executed verbatim in a faithful temp reproduction of
    the job state; the frozen contract: it exits 0.
    """
    if BASH is None:  # pragma: no cover - CI always has bash
        pytest.skip("no bash available to execute the workflow step script")
    step_script = _step_run(
        "mutation-backend",
        "Report per-module mutation scores and enforce the survival budget",
    )
    # Real guard-module pair from the workflow's guard_modules list.
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
    env = _bash_env()
    env["MUTATION_SCORE_FLOOR"] = "30"  # the workflow's env pin
    result = subprocess.run(
        [BASH, "-c", step_script],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, (
        "mutation-score step must exit 0 for an above-floor stats file; it "
        f"exited {result.returncode}\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_mutmut_source_paths_resolve_from_backend_cwd(tmp_path):
    """#739-AC3: mutmut step source_paths must resolve from the job cwd.

    The step runs with working-directory ``backend`` but rewrites pyproject
    ``source_paths`` with repo-root-prefixed paths (``backend/app/...``), so
    mutmut inside the backend dir cannot resolve them. The step script is
    executed verbatim (mutmut stubbed); the frozen contract: every
    ``source_paths`` entry the step wrote resolves relative to the temp
    ``backend/`` directory.
    """
    if BASH is None:  # pragma: no cover - CI always has bash
        pytest.skip("no bash available to execute the workflow step script")
    step_script = _step_run(
        "mutation-backend",
        "Run mutmut per guard module and export per-module stats",
    )
    # Real guard-module pair from the workflow's guard_modules list.
    module, module_path = (
        "app.middleware.logging",
        "backend/app/middleware/logging.py",
    )
    backend_dir = tmp_path / "backend"
    _write_lf(backend_dir / "pyproject.toml", '[project]\nname = "probe"\n')
    _write_lf(backend_dir / "mutation_modules.txt", f"{module} {module_path}\n")
    # The guard module file itself, so a correctly-scoped source_paths entry
    # resolves on disk.
    _write_lf(backend_dir / "app" / "middleware" / "logging.py", "X = 1\n")
    # Stub mutmut package: placed inside the backend dir (cwd is on sys.path
    # for ``python -m``) AND first on PYTHONPATH, so the stub always wins
    # over any real mutmut install. It no-ops both ``run`` and
    # ``export-cicd-stats`` (tolerating the step's output redirection).
    stub_pkg = backend_dir / "mutmut"
    _write_lf(stub_pkg / "__init__.py", "")
    _write_lf(
        stub_pkg / "__main__.py",
        "import sys\nsys.exit(0)\n",
    )
    env = _bash_env()
    env["PYTHONPATH"] = str(backend_dir)
    result = subprocess.run(
        [BASH, "-c", step_script],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    pyproject_text = (backend_dir / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"source_paths\s*=\s*\[(.*?)\]", pyproject_text, re.DOTALL)
    assert match is not None, (
        f"step did not write a source_paths array; pyproject is "
        f"{pyproject_text!r}; step stderr:\n{result.stderr}"
    )
    entries = re.findall(r'"([^"]+)"', match.group(1))
    unresolved = [e for e in entries if not (backend_dir / e).is_file()]
    assert unresolved == [], (
        "mutmut source_paths entries must resolve relative to the backend "
        f"working directory (step rc={result.returncode}); unresolved: "
        f"{unresolved}"
    )


def test_stryker_job_has_timeout_headroom():
    """#739-AC4: stryker-frontend needs >= 180 timeout-minutes or a matrix.

    The job runs the whole frontend mutation suite serially; at the base
    timeout of 120 minutes a slow run is killed mid-mutation with no way to
    shard. Frozen contract: timeout-minutes >= 180 OR the job defines a
    ``strategy.matrix`` that shards the work.
    """
    job = _nightly_gates_workflow()["jobs"]["stryker-frontend"]
    ok = job.get("timeout-minutes", 0) >= 180 or bool(
        job.get("strategy", {}).get("matrix")
    )
    assert ok is True, (
        "stryker-frontend has neither >= 180 timeout-minutes "
        f"(has {job.get('timeout-minutes')}) nor a strategy matrix to shard "
        "the run"
    )


def test_schemathesis_exit_code_survives_pipefail(tmp_path):
    """#739-AC5: SCHEMATHESIS_EXIT must record the tool's real exit code.

    The step runs under GitHub Actions' default ``bash -eo pipefail``. The
    ``|| true`` guard resets PIPESTATUS before ``rc=${PIPESTATUS[0]}`` runs,
    so a crashed schemathesis run is recorded as SCHEMATHESIS_EXIT=0 and the
    downstream gate can never see the crash. The step script is executed
    verbatim with a ``python`` shim that exits 3 first on PATH; frozen
    contract: schemathesis-run.log records ``SCHEMATHESIS_EXIT=3``.
    """
    if BASH is None:  # pragma: no cover - CI always has bash
        pytest.skip("no bash available to execute the workflow step script")
    step_script = _step_run(
        "schemathesis-api",
        "Run Schemathesis against /openapi.json with stateful links",
    )
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(parents=True)
    (shim_dir / "python").write_text(
        "#!/usr/bin/env bash\nexit 3\n", encoding="utf-8", newline="\n"
    )
    os.chmod(shim_dir / "python", 0o755)
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    subprocess.run(
        [BASH, "-eo", "pipefail", "-c", step_script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    log_path = tmp_path / "schemathesis-run.log"
    assert log_path.is_file(), "the step did not write schemathesis-run.log"
    log_text = log_path.read_text(encoding="utf-8")
    markers = re.findall(r"SCHEMATHESIS_EXIT=(\d+)", log_text)
    assert markers, f"no SCHEMATHESIS_EXIT marker in log: {log_text!r}"
    assert int(markers[-1]) == 3, (
        "SCHEMATHESIS_EXIT must preserve schemathesis's exit code (3) under "
        f"-eo pipefail; recorded {markers[-1]} (log: {log_text!r})"
    )


def test_runtime_contract_checks_every_workflow_file(tmp_path):
    """#739-AC7: the runtime contract must version-check every workflow.

    scripts/check_runtime_contract.py's check_ci_versions reads only
    .github/workflows/ci.yml, so a stale python pin in a scheduled workflow
    (nightly.yml) passes the gate. Frozen contract: with a temp ROOT holding
    the real ci.yml plus a minimal nightly.yml whose only version content is
    one ``python-version: "3.12"`` pin, check_ci_versions reports exactly one
    failure naming .github/workflows/nightly.yml.
    """
    runtime_contract = _load_script("check_runtime_contract.py")
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    shutil.copy(WORKFLOWS / "ci.yml", workflows_dir / "ci.yml")
    _write_lf(
        workflows_dir / "nightly.yml",
        (
            "name: Nightly probe\n"
            "on:\n"
            "  schedule:\n"
            '    - cron: "0 3 * * *"\n'
            "jobs:\n"
            "  nightly:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/setup-python@v7\n"
            "        with:\n"
            '          python-version: "3.12"\n'
        ),
    )
    failures: list[str] = []
    monkeypatch_root = pytest.MonkeyPatch()
    monkeypatch_root.setattr(runtime_contract, "ROOT", tmp_path)
    try:
        runtime_contract.check_ci_versions(failures)
    finally:
        monkeypatch_root.undo()
    naming = [f for f in failures if ".github/workflows/nightly.yml" in f]
    assert len(naming) == 1, (
        "check_ci_versions must version-check every workflow file: expected "
        f"exactly 1 failure naming the seeded nightly.yml 3.12 pin, got "
        f"{len(naming)} (failures: {failures})"
    )


def test_runtime_contract_rejects_stale_digest(tmp_path):
    """#739-AC8: the runtime contract must reject a placeholder digest pin.

    FROM_RE treats the ``@sha256:`` digest group as optional noise, so a
    Dockerfile FROM line pinned to an all-zero (placeholder/stale) digest is
    accepted as if it were properly pinned. Frozen contract: with a temp ROOT
    holding a Dockerfile whose single line is
    ``FROM python:3.11-slim@sha256:<64 zeros>``, check_dockerfiles reports
    exactly one failure naming that Dockerfile.
    """
    runtime_contract = _load_script("check_runtime_contract.py")
    (tmp_path / "Dockerfile").write_text(
        f"FROM python:3.11-slim@sha256:{'0' * 64}\n",
        encoding="utf-8",
        newline="\n",
    )
    failures: list[str] = []
    monkeypatch_root = pytest.MonkeyPatch()
    monkeypatch_root.setattr(runtime_contract, "ROOT", tmp_path)
    try:
        runtime_contract.check_dockerfiles(failures)
    finally:
        monkeypatch_root.undo()
    naming = [f for f in failures if f.startswith("Dockerfile:")]
    assert len(naming) == 1, (
        "check_dockerfiles must reject the all-zero placeholder digest pin: "
        f"expected exactly 1 failure naming the Dockerfile, got "
        f"{len(naming)} (failures: {failures})"
    )
