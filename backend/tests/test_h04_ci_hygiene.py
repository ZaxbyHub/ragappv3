"""Acceptance checks for issue #742 (workstream H, PR 4 of 10): CI job
timeouts, job-scope isolation, devcontainer Node floor.

Scope — the mechanical rows this PR delivers, keyed to the issue's rows:

* PRESERVING rows (GREEN before and after; delivered by PR #1034, pinned
  here so they cannot regress silently): job-level ``timeout-minutes`` on
  the ``changes`` / ``frontend`` / ``quality-contracts`` CI jobs (A1) and a
  hang-dumping pytest timeout mechanism on the Backend Test step (A3).
* DISCRIMINATING rows (RED at base, GREEN after the fix): a step-level
  ``timeout-minutes`` on the Backend pytest step, strictly inside the job's
  60-minute budget (A2); no frontend-toolchain installs inside the Backend
  job, which is what lets the issue-258 frontend-toolchain tests run there
  instead of in exactly one CI job (A5); the devcontainer Node pin
  satisfying jsdom 30's transitive engines range (A6); frontend/package.json
  ``engines.node`` excluding the lockfile-rejected 22.22.0/22.22.1 patches
  (A7); and the four prose/Docker files no longer naming those rejected
  patches (A8).

The workflow is parsed structurally with PyYAML (available in the backend
test env); the semver rows use a tiny evaluator that supports exactly the
range shapes this repo pins (``^X.Y.Z``, ``>=M[.m[.p]]``, ``||`` unions).
No network, no subprocesses.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "backend" / "pyproject.toml"
DEVCONTAINER = REPO_ROOT / ".devcontainer" / "devcontainer.json"
FRONTEND_PACKAGE = REPO_ROOT / "frontend" / "package.json"
GUARD_FILES = (
    REPO_ROOT / "backend" / "tests" / "test_issue258_coverage_gate.py",
    REPO_ROOT / "backend" / "tests" / "test_issue258_build_contracts.py",
)
NODE_PATCH_DOC_FILES = ("CONTRIBUTING.md", "INSTALLATION.md", "Dockerfile", "frontend/Dockerfile")

# jsdom 30's transitive engines range for node (the lockfile's dependency
# engines; enforced by .devcontainer/post-create.sh's `npm ci --engine-strict`,
# so a pin outside this range fails every fresh devcontainer build).
JSDOM_NODE_ENGINES = "^22.22.2 || ^24.15.0 || >=26"

# Patch releases the lockfile's dependency engines reject (issue #742 A7/A8).
REJECTED_NODE_PATCHES = ("22.22.0", "22.22.1")


def _load_ci() -> dict:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def _backend_pytest_steps(ci: dict) -> list[dict]:
    """Steps of the backend job whose run command is a bare pytest invocation."""
    steps = ci["jobs"]["backend"]["steps"]
    return [
        step
        for step in steps
        if str(step.get("run") or "").strip().startswith("pytest")
        and "tests/" in str(step.get("run") or "")
    ]


def test_unbounded_ci_jobs_have_timeouts():
    # A1 (PRESERVING — GREEN before and after; delivered by PR #1034): the
    # three previously-unbounded CI jobs carry job-level timeout-minutes.
    ci = _load_ci()
    jobs = ci["jobs"]
    scoped = {"changes", "frontend", "quality-contracts"}
    lacking = sorted(
        job_id for job_id in scoped if not isinstance(jobs[job_id].get("timeout-minutes"), int)
    )
    assert sorted(lacking) == []


def test_backend_pytest_step_has_step_timeout():
    # A2 (DISCRIMINATING — RED at base, GREEN after the fix): the Backend
    # Test (pytest) step must carry its own timeout-minutes, strictly inside
    # the job's budget, so a wedged pytest run cannot burn the whole job.
    ci = _load_ci()
    matches = _backend_pytest_steps(ci)
    assert matches, "backend pytest (Test) step not found in ci.yml"
    step = matches[0]
    job_timeout = ci["jobs"]["backend"]["timeout-minutes"]
    bounded = (
        isinstance(step.get("timeout-minutes"), int)
        and 0 < step["timeout-minutes"] < job_timeout
    )
    assert bounded is True


def _pytest_ini_options_text() -> str:
    """The [tool.pytest.ini_options] table body via a simple line scan."""
    captured: list[str] = []
    inside = False
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            if inside:
                break  # the next TOML table started
            inside = line.strip() == "[tool.pytest.ini_options]"
        elif inside:
            captured.append(line)
    return "\n".join(captured)


def test_backend_pytest_hang_dumps_stacks():
    # A3 (PRESERVING — GREEN before and after; delivered by PR #1034): a hung
    # backend test must dump stacks. Accepted forms: --timeout-method=thread
    # in the step, -o faulthandler_timeout= in the step, or
    # faulthandler_timeout in backend/pyproject.toml [tool.pytest.ini_options].
    ci = _load_ci()
    matches = _backend_pytest_steps(ci)
    assert matches, "backend pytest (Test) step not found in ci.yml"
    run = str(matches[0].get("run") or "")
    accepted = (
        "--timeout-method=thread" in run
        or "-o faulthandler_timeout=" in run
        or "faulthandler_timeout" in _pytest_ini_options_text()
    )
    assert accepted is True


def _npm_step_installs_into_frontend_tree(step: dict) -> bool:
    """True when a step's npm install/ci targets the repo's frontend tree.

    Computation (kept deliberately simple): the step is in scope when its
    run invokes ``npm install`` / ``npm ci`` AND its effective install
    location is the in-repo frontend tree — i.e. step ``working-directory``
    is frontend (or under it) and any ``--prefix`` argument does not point
    outside it ($RUNNER_TEMP/... or any non-frontend prefix counts as
    outside, so an install parked off-repo is fine).
    """
    run = str(step.get("run") or "")
    if "npm install" not in run and "npm ci" not in run:
        return False
    prefix = re.search(r"--prefix[ =](\S+)", run)
    if prefix and "frontend" not in prefix.group(1):
        return False  # install parked outside the repo tree
    working_directory = str(step.get("working-directory") or "")
    return working_directory == "frontend" or working_directory.startswith("frontend/")


def _skip_guards_env_keyed(ci: dict) -> bool:
    """True when BOTH issue-258 skip guards read an env var the backend job sets.

    The alternative to removing the in-repo npm install is making the two
    issue-258 frontend-toolchain nodes opt-in via an env var the backend job
    sets (they currently key on ``frontend/node_modules`` existing — which is
    exactly what the backend job's install creates). Detected by grepping
    both guard files for an os.environ/os.getenv read while the backend job
    sets at least one env key.
    """
    backend_env_keys = {
        str(key)
        for step in ci["jobs"]["backend"]["steps"]
        for key in (step.get("env") or {})
    }
    if not backend_env_keys:
        return False
    return all(
        re.search(r"os\.(?:environ|getenv)", path.read_text(encoding="utf-8"))
        for path in GUARD_FILES
    )


def test_backend_job_does_not_run_frontend_toolchain_tests():
    # A5 (DISCRIMINATING — RED at base, GREEN after the fix): the Backend job
    # must not install the frontend dependency tree in-repo (npm install/ci
    # with working-directory frontend), and the two issue-258 frontend-
    # toolchain nodes must not silently ride that install into the job.
    ci = _load_ci()
    steps = ci["jobs"]["backend"]["steps"]
    scoped = (
        not any(_npm_step_installs_into_frontend_tree(step) for step in steps)
        or _skip_guards_env_keyed(ci)
    )
    assert scoped is True


def _version_tuple(text: str) -> tuple[int, ...]:
    """'22.22.0' -> (22, 22, 0); tolerant of a pre-release suffix piece."""
    parts = []
    for piece in text.strip().split("."):
        digits = re.match(r"\d+", piece)
        parts.append(int(digits.group(0)) if digits else 0)
    return tuple(parts)


def _satisfies_range(version: tuple[int, ...], range_text: str) -> bool:
    """Tiny semver evaluator for exactly the shapes this repo pins.

    Supports ``||`` unions of ``^X.Y.Z`` (>= X.Y.Z and < (X+1).0.0) and
    ``>=`` lower bounds with 1-3 components (``>=26`` means major >= 26).
    """
    for alternative in range_text.split("||"):
        alternative = alternative.strip()
        if alternative.startswith("^"):
            target = _version_tuple(alternative[1:])
            if version >= target and version[0] == target[0]:
                return True
        elif alternative.startswith(">="):
            if version >= _version_tuple(alternative[2:]):
                return True
    return False


def test_devcontainer_node_satisfies_jsdom_engines():
    # A6 (DISCRIMINATING — RED at base, GREEN after the fix): the
    # devcontainer's pinned node version must satisfy jsdom 30's engines
    # range, or every fresh devcontainer build fails EBADENGINE at
    # `npm ci --engine-strict`.
    devcontainer = json.loads(DEVCONTAINER.read_text(encoding="utf-8"))
    pinned = devcontainer["features"]["ghcr.io/devcontainers/features/node:1"]["version"]
    ok = _satisfies_range(_version_tuple(pinned), JSDOM_NODE_ENGINES)
    assert ok is True


def test_package_engines_excludes_rejected_node_patches():
    # A7 (DISCRIMINATING — RED at base, GREEN after the fix): the frontend
    # engines range must not admit node versions the lockfile's dependency
    # engines reject (22.22.0 and 22.22.1).
    package = json.loads(FRONTEND_PACKAGE.read_text(encoding="utf-8"))
    engines = str(package["engines"]["node"])
    admitted = sorted(
        probe
        for probe in REJECTED_NODE_PATCHES
        if _satisfies_range(_version_tuple(probe), engines)
    )
    assert admitted == []


def test_docs_do_not_admit_rejected_node_patches():
    # A8 (DISCRIMINATING — RED at base, GREEN after the fix): prose and
    # Dockerfiles must stop telling humans to install the rejected patches.
    mentioning = sorted(
        rel
        for rel in NODE_PATCH_DOC_FILES
        if any(
            patch in (REPO_ROOT / rel).read_text(encoding="utf-8")
            for patch in REJECTED_NODE_PATCHES
        )
    )
    assert mentioning == []
