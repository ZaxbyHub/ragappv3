"""Frozen issue-tracer checks for issue #751 (the shipped dependency lock).

Acceptance checks AC1-AC4 of the #751 remediation plan:

- AC1: a PR that changes only ``backend/requirements.txt`` or
  ``backend/requirements-lock.txt`` must select the docker-smoke job via
  ci.yml's docker-scope paths filter (the image installs
  ``requirements-lock.txt``, so a lock-only change is a docker-surface
  change).
- AC2: at least one workflow job must install the SHIPPED
  ``requirements-lock.txt`` with ``--require-hashes`` and then run pytest,
  so "green" somewhere exercises the pinned set that Docker actually ships
  (not the unlocked source specs).
- AC3: every tracked ``*.sh`` must be stored with LF line endings in the
  git index (``scripts/update-lock.sh`` is committed with ``i/crlf``,
  which makes Linux bash abort at ``set -euo pipefail``).
- AC4: a ``.gitattributes`` rule must force ``eol=lf`` for ``*.sh`` so a
  later CRLF commit cannot reintroduce the defect.

AC5 is NON-EXECUTABLE (host-only: the CRLF failure reproduces on Linux
bash, not on Windows Git Bash). AC6 is PRESERVING and stays pinned by the
existing suite nodes ``tests/test_issue258_build_contracts.py::
test_ac19_lockfile_entries_all_hash_pinned`` and ``::
test_ac19_lockfile_regen_procedure_documented`` — it is not re-implemented
here.

AC1-AC4 are RED at the recorded base tree and turn GREEN when the #751
fix lands.
"""

import subprocess
from fnmatch import fnmatch
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_SUBPROCESS_TIMEOUT_SECONDS = 60

_REQUIREMENTS_INPUTS = [
    "backend/requirements.txt",
    "backend/requirements-lock.txt",
]


def _git(argv: list[str]) -> str:
    """Run git from the repo root and return stdout (list argv, no shell)."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *argv],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=True,
    )
    return result.stdout


def _docker_filter_patterns() -> list[str]:
    """Path patterns of the ``docker`` filter in ci.yml's ``changes`` job.

    The dorny/paths-filter ``filters`` input is a YAML string mapping a
    filter name to a path list; both layers are parsed with ``safe_load``.
    """
    ci = yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))
    changes_job = (ci.get("jobs") or {}).get("changes") or {}
    filters_text = None
    for step in changes_job.get("steps") or []:
        with_block = (step or {}).get("with") or {}
        if "filters" in with_block:
            filters_text = with_block["filters"]
            break
    assert filters_text, "ci.yml changes job must define a paths-filter block"
    filters = yaml.safe_load(filters_text)
    return list(filters.get("docker") or [])


# ── AC1: docker-scope filter covers the image's requirements inputs ─────────


def test_docker_scope_filter_covers_image_requirements():
    patterns = _docker_filter_patterns()
    uncovered = [
        path
        for path in _REQUIREMENTS_INPUTS
        if not any(fnmatch(path, pattern) for pattern in patterns)
    ]
    assert uncovered == [], (
        "ci.yml's docker paths-filter must match the requirements files the "
        f"image installs (uncovered: {uncovered}); a lock-only change is a "
        "docker-surface change because the Dockerfile pip-installs "
        "requirements-lock.txt"
    )


# ── AC2: some workflow job tests the shipped lock ───────────────────────────


def _job_run_lines(job: dict) -> list[str]:
    """A job's ``run`` step lines, in step order (multi-line runs flattened)."""
    lines: list[str] = []
    for step in job.get("steps") or []:
        run = (step or {}).get("run")
        if isinstance(run, str):
            lines.extend(run.splitlines())
    return lines


def test_some_workflow_tests_the_shipped_lock():
    qualifying_jobs = []
    for workflow in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            lines = _job_run_lines(job)
            for index, line in enumerate(lines):
                installs_shipped_lock = (
                    "pip install" in line
                    and "--require-hashes" in line
                    and "requirements-lock.txt" in line
                    and "requirements-lock-ci.txt" not in line
                )
                if installs_shipped_lock and any(
                    "pytest" in later for later in lines[index + 1 :]
                ):
                    qualifying_jobs.append(f"{workflow.name}:{job_name}")
                    break
    assert len(qualifying_jobs) >= 1, (
        "at least one workflow job must pip-install the shipped "
        "requirements-lock.txt with --require-hashes and then run pytest; "
        "no job does (every tier installs requirements.txt or the CI lock), "
        "so green CI proves nothing about the set Docker ships"
    )


# ── AC3: no tracked *.sh blob is CRLF in the git index ──────────────────────


def test_no_tracked_shell_script_has_crlf_in_index():
    # The index column is checked, not the working tree: Windows checkouts
    # legitimately show w/crlf for every file.
    crlf_paths = []
    for line in _git(["ls-files", "--eol", "--", "*.sh"]).splitlines():
        tokens = line.split()
        if tokens and tokens[0] == "i/crlf":
            crlf_paths.append(line.strip())
    assert len(crlf_paths) == 0, (
        "every tracked *.sh must be stored LF in the git index (i/crlf "
        f"found: {crlf_paths}); CRLF shell scripts abort under Linux bash "
        "with 'set: pipefail: invalid option name'"
    )


# ── AC4: .gitattributes pins eol=lf for shell scripts ───────────────────────


def test_gitattributes_pins_lf_for_shell_scripts():
    stdout = _git(["check-attr", "eol", "--", "scripts/update-lock.sh"])
    value = ""
    for line in stdout.splitlines():
        if ": eol: " in line:
            value = line.rsplit(": eol: ", 1)[1].strip()
    assert value == "lf", (
        "git must resolve eol=lf for scripts/update-lock.sh via a tracked "
        f".gitattributes rule; git reports {value!r} instead"
    )
