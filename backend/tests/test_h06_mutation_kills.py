"""Frozen acceptance checks for the collated #743/#744 issue-tracer trace (H06).

Issue #743 — test-integrity mutation kills. Each mutating check copies the
repo's ``backend/`` and ``scripts/`` trees into ``tmp_path``, applies one
textual mutation to a production file, runs the affected suite(s) with pytest
inside the copy, and asserts the mutation is KILLED (at least one test
fails). The structural check (``test_otel_guard_runs_unskipped_in_some_workflow``)
pins the CI wiring for the OpenTelemetry guard.

These checks are RED at base and turn GREEN after the hardening fix lands:
at base every mutated suite still passes, and no workflow installs
``backend/requirements-otel.txt``, so the assertions ``n_failed >= 1`` /
``count >= 1`` fail. No network access; all writes stay inside ``tmp_path``.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Frozen ignore set for the mutation-copy harness (shared verbatim by every
# mutating check in this file). Local-only junk such as backend/.swarm
# (gitignored pytest-sweep artifacts, ~1 GB) is excluded too: no suite reads
# it and copying it would cost ~10 s and ~1 GB of disk per mutation run.
_COPY_IGNORE = shutil.ignore_patterns(
    ".venv",
    "venv",
    "data",
    "__pycache__",
    ".pytest_cache",
    ".swarm",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage*",
)
_SUBPROCESS_TIMEOUT_S = 600


def _read_source(path: Path) -> str:
    """Read a file byte-faithfully (no newline translation)."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write_source(path: Path, text: str) -> None:
    """Write a file byte-faithfully (preserve the line endings read)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _prepare_copy(tmp_path: Path) -> Path:
    """Copy backend/ and scripts/ into tmp_path and return the copy root."""
    copy_root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "backend", copy_root / "backend", ignore=_COPY_IGNORE)
    shutil.copytree(REPO_ROOT / "scripts", copy_root / "scripts", ignore=_COPY_IGNORE)
    return copy_root


def _mutate(
    copy_root: Path,
    relpath: str,
    anchor: str,
    mutated: str,
    *,
    replace_all: bool = False,
) -> int:
    """Apply one textual mutation to <copy>/backend/<relpath>.

    Asserts the anchor occurs at least once so a moved/renamed anchor errors
    loudly instead of silently passing the kill check.
    """
    target = copy_root / "backend" / relpath
    src = _read_source(target)
    occurrences = src.count(anchor)
    assert occurrences >= 1, (
        f"MUTATION ANCHOR MISSING (harness problem, not a mutant-survival "
        f"result): anchor {anchor!r} does not occur in backend/{relpath}. "
        f"Update the anchor per the localization log."
    )
    if replace_all:
        _write_source(target, src.replace(anchor, mutated))
    else:
        _write_source(target, src.replace(anchor, mutated, 1))
    return occurrences


def _run_suite(
    copy_root: Path,
    suite: list,
    tmp_path: Path,
    check_name: str,
) -> int:
    """Run pytest on ``suite`` inside <copy>/backend and assert >=1 failure."""
    argv = [
        sys.executable,
        "-m",
        "pytest",
        *suite,
        "-q",
        "-p",
        "no:randomly",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(tmp_path / "bt"),
    ]
    proc = subprocess.run(
        argv,
        cwd=str(copy_root / "backend"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    match = re.search(r"(\d+) failed", proc.stdout)
    n_failed = int(match.group(1)) if match else 0
    tail = "\n".join(proc.stdout.splitlines()[-15:])
    assert n_failed >= 1, (
        f"{check_name}: the mutant SURVIVED — the suite passed with the "
        f"mutation applied.\n"
        f"suite: {suite}\n"
        f"pytest exit code: {proc.returncode}\n"
        f"--- suite output tail ---\n{tail}"
    )
    return n_failed


def test_org_member_removal_mutant_is_killed(tmp_path):
    """#743 AC1: remove-org-member must actually delete the org_members row.

    Mutates the DELETE statement in app/api/routes/organizations.py to a
    no-op SELECT that still lets the route answer 200 "Member removed"; the
    org/user-org route suites must then fail at least one test.
    """
    copy_root = _prepare_copy(tmp_path)
    _mutate(
        copy_root,
        "app/api/routes/organizations.py",
        '"DELETE FROM org_members WHERE org_id = ? AND user_id = ?",',
        '"SELECT ?, ?",',
    )
    _run_suite(
        copy_root,
        [
            "tests/test_organizations_routes.py",
            "tests/test_user_org_roles.py",
            "tests/test_user_organizations_scoped.py",
        ],
        tmp_path,
        "org-member-removal mutant (issue #743 AC1)",
    )


def test_lifespan_shutdown_stop_mutant_is_killed(tmp_path):
    """#743 AC3: skipping background_processor.stop() in lifespan shutdown.

    Replaces the ``await app.state.background_processor.stop()`` line in
    app/lifespan.py with ``pass``; every tests/*lifespan*.py file (resolved
    by glob in the copy, so new lifespan test files count) must fail at
    least one test.
    """
    copy_root = _prepare_copy(tmp_path)
    _mutate(
        copy_root,
        "app/lifespan.py",
        "        await app.state.background_processor.stop()",
        "        pass",
    )
    suite = sorted(
        f"tests/{path.name}"
        for path in (copy_root / "backend" / "tests").glob("*lifespan*.py")
    )
    assert suite, "no tests/*lifespan*.py files found in the copy"
    _run_suite(
        copy_root,
        suite,
        tmp_path,
        "lifespan shutdown-stop mutant (issue #743 AC3)",
    )


def test_request_id_filter_commented_out_mutant_is_killed(tmp_path):
    """#743 AC4: commenting out both RequestIdFilter registrations.

    Replaces both addFilter(RequestIdFilter()) call sites (fresh-handler and
    existing-handler branches) in app/lifespan.py with ``pass  # ...``
    comments; the telemetry suites must fail at least one test. A
    source-substring or occurrence-count assertion is still satisfied by the
    commented-out text, so only a runtime registration check kills this.
    """
    copy_root = _prepare_copy(tmp_path)
    _mutate(
        copy_root,
        "app/lifespan.py",
        "_handler.addFilter(RequestIdFilter())",
        "pass  # _handler.addFilter(RequestIdFilter())",
        replace_all=True,
    )
    _mutate(
        copy_root,
        "app/lifespan.py",
        "_existing.addFilter(RequestIdFilter())",
        "pass  # _existing.addFilter(RequestIdFilter())",
        replace_all=True,
    )
    _run_suite(
        copy_root,
        ["tests/test_518_telemetry.py", "tests/test_518_review_hardening.py"],
        tmp_path,
        "RequestIdFilter commented-out mutant (issue #743 AC4)",
    )


def test_sensitive_field_filter_commented_out_mutant_is_killed(tmp_path):
    """#743 AC5: commenting out both SensitiveFieldFilter registrations.

    Same shape as the RequestIdFilter check, for the PII redaction filter:
    both addFilter(SensitiveFieldFilter()) call sites in app/lifespan.py are
    commented out and the telemetry suites must fail at least one test.
    """
    copy_root = _prepare_copy(tmp_path)
    _mutate(
        copy_root,
        "app/lifespan.py",
        "_handler.addFilter(SensitiveFieldFilter())",
        "pass  # _handler.addFilter(SensitiveFieldFilter())",
        replace_all=True,
    )
    _mutate(
        copy_root,
        "app/lifespan.py",
        "_existing.addFilter(SensitiveFieldFilter())",
        "pass  # _existing.addFilter(SensitiveFieldFilter())",
        replace_all=True,
    )
    _run_suite(
        copy_root,
        ["tests/test_518_telemetry.py", "tests/test_518_review_hardening.py"],
        tmp_path,
        "SensitiveFieldFilter commented-out mutant (issue #743 AC5)",
    )


def test_reindex_lease_worker_loop_noop_mutant_is_killed(tmp_path):
    """#743 AC6: the reindex lease worker loop must be driven by a test.

    Inserts an immediate ``return`` right after the docstring of
    ``_reindex_lease_worker_loop`` in app/services/background_tasks.py (the
    docstring's opening line is the anchor; the return lands after its
    closing quotes), making the loop a no-op; the #559 lease suites must
    fail at least one test.
    """
    copy_root = _prepare_copy(tmp_path)
    target = copy_root / "backend" / "app" / "services" / "background_tasks.py"
    docstring_open = '        """Claim-and-run reindex jobs through the shared lease.'
    src = _read_source(target)
    occurrences = src.count(docstring_open)
    assert occurrences >= 1, (
        f"MUTATION ANCHOR MISSING (harness problem, not a mutant-survival "
        f"result): anchor {docstring_open!r} does not occur in "
        f"backend/app/services/background_tasks.py."
    )
    open_idx = src.find(docstring_open)
    close_idx = src.find('"""', open_idx + len(docstring_open))
    assert close_idx != -1, "closing quotes of the worker-loop docstring not found"
    insert_at = close_idx + 3
    _write_source(
        target, src[:insert_at] + "\n        return" + src[insert_at:]
    )
    _run_suite(
        copy_root,
        ["tests/test_559_c05_proving.py", "tests/test_559_s3_c5_reindex_lease.py"],
        tmp_path,
        "reindex lease worker loop no-op mutant (issue #743 AC6)",
    )


_JOB_KEY_RE = re.compile(r"^  [A-Za-z0-9_-]+:")


def _workflow_job_blocks(text):
    """Split a GitHub Actions workflow into per-job line blocks.

    A block starts at a line with exactly two spaces of indentation whose
    first token is a key under the top-level ``jobs:`` mapping, and ends at
    the next such key or the next top-level (column-0) key.
    """
    blocks = []
    current = None
    in_jobs = False
    for line in text.splitlines():
        if not line.strip():
            if current is not None:
                current.append(line)
            continue
        if not line[0].isspace():
            in_jobs = line.startswith("jobs:")
            current = None
            continue
        if in_jobs and _JOB_KEY_RE.match(line):
            current = [line]
            blocks.append(current)
        elif current is not None:
            current.append(line)
    return blocks


def test_otel_guard_runs_unskipped_in_some_workflow():
    """#743 AC7: some workflow job must run the OTel guard unskipped.

    Counts workflow jobs that BOTH install backend/requirements-otel.txt AND
    run a pytest target that includes tests/ or test_518_closure_units.py.
    Without such a job, TestRealTracerPipeline skips on every single run
    (the opentelemetry SDK is never installed), permanently disabling the
    F-001c held-span-lifetime regression guard.
    """
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    yml_files = sorted(workflows_dir.glob("*.yml"))
    assert yml_files, f"no .github/workflows/*.yml files found under {workflows_dir}"
    count = 0
    for yml_path in yml_files:
        text = yml_path.read_text(encoding="utf-8")
        for block in _workflow_job_blocks(text):
            installs_otel = any(
                "requirements-otel.txt" in line for line in block
            )
            runs_closure_units = any(
                "pytest" in line
                and ("tests/" in line or "test_518_closure_units.py" in line)
                for line in block
            )
            if installs_otel and runs_closure_units:
                count += 1
    assert count >= 1, (
        "no workflow job both installs backend/requirements-otel.txt and "
        "runs a pytest target including test_518_closure_units.py, so the "
        "OTel guard (TestRealTracerPipeline) is skipped on every run; "
        f"jobs found with an otel install: {count}"
    )
