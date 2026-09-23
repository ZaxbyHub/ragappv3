#!/usr/bin/env python3
"""Fail when a pytest test file lives outside the collected test roots (issue #563 / C11).

backend/pyproject.toml sets ``testpaths = ["tests"]`` and CI runs
``pytest ... tests/``, so a ``test_*.py`` file anywhere else is dead weight:
never collected, never executed in CI, silently rotting. Both root-level files
this guard was written for had drifted exactly that way until #563 (one called
a ``BackgroundProcessor.enqueue`` signature that no longer exists; neither was
run by any test invocation since 2026-04).

Contracts enforced:

  C-TESTSCOPE-1: every ``test_*.py`` / ``*_test.py`` file in the repository
      must live under ``backend/tests/``.

Scope note (issue #563 wording): the issue's sanctioned-root phrasing is
"outside backend/tests/ or frontend/src". The frontend convention names its
tests ``*.test.ts``/``*.test.tsx`` under ``frontend/src/`` — those are not
pytest files and are invisible to this contract by construction, so no
frontend allowlist entry is needed; only Python test filenames are policed.

Candidate enumeration (git mode — issue #656): ``git ls-files`` (tracked) plus
``git ls-files --others --exclude-standard`` (untracked, not ignored). That is
git's view of the repository — the set this contract is about: files a
contributor could commit and that ``pytest tests/`` would silently never
collect. A raw filesystem walk cannot express that set: it sees gitignored
local artifacts (pytest-xdist leftovers under the ignored ``backend/data/``
made the gate exit 1 on any developer machine that had run the suite) while
missing nothing else. Candidates must also exist on disk, so a tracked file
deleted from the working tree is not an on-disk violation. Hidden directories
(``.git``, ``.venv*``, ``.agents/issue-traces``, ...) stay pruned: vendored,
generated, virtualenv, and agent-artifact trees are not pytest collection
surfaces. ``PRUNE_DIRS`` is NOT applied in git mode — git already excludes
ignored venvs, and a *tracked* venv's test files are real violations.

Degraded mode: if git is unavailable (binary missing), hung, or fails (for
example outside a git checkout), the original filesystem walk runs instead —
hidden directories, ``node_modules``, ``__pycache__``, and the non-hidden
virtualenv names ``venv``/``env``/``ENV`` pruned (INSTALLATION.md's
``python -m venv venv`` is non-hidden and ships hundreds of packaged test
files inside site-packages) — and a one-line note is printed to stderr.

Exit codes: 0 = clean, 1 = violations found (each offender printed).
Run from any cwd (paths resolve from this file).
"""

import subprocess
import sys
from pathlib import Path

PRUNE_DIRS = {"__pycache__", "node_modules", "venv", "env", "ENV"}
TEST_FILE_SUFFIXES = (".py",)
GIT_TIMEOUT_SECONDS = 60
DEGRADED_NOTE = "test-collection-scope: git unavailable; falling back to filesystem walk"


def _is_test_filename(name: str) -> bool:
    return (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(
        TEST_FILE_SUFFIXES
    )


def _git_paths(repo_root: Path, *args: str) -> list[str] | None:
    """Return git's answer as repo-relative posix paths, or None.

    None means git could not answer (binary missing, hung past
    ``GIT_TIMEOUT_SECONDS``, or non-zero exit) and the caller must fall back to
    the filesystem walk. Tokens are the raw NUL-delimited fields with only the
    empty terminal token discarded: git permits leading/trailing whitespace in
    path components, and stripping would resolve them to non-existent paths.

    Output is decoded with ``errors="surrogateescape"``: git emits raw bytes and
    a non-UTF-8 path component would otherwise raise UnicodeDecodeError (or, on
    Windows, surface as ``stdout=None``) and crash the gate instead of
    degrading. Surrogate-escaped tokens still round-trip through ``Path`` and
    the filesystem unchanged. ``ValueError`` is caught alongside the OS/timeout
    errors as defense in depth for any future decode misconfiguration.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if proc.returncode != 0 or proc.stdout is None:
        return None
    return [token for token in proc.stdout.split("\0") if token]


def _has_hidden_parent(rel_posix: str) -> bool:
    return any(part.startswith(".") for part in Path(rel_posix).parts[:-1])


def _is_under_backend_tests(rel_posix: str) -> bool:
    parts = Path(rel_posix).parts
    return len(parts) >= 2 and parts[0] == "backend" and parts[1] == "tests"


def _walk_violations(repo_root: Path) -> list[str]:
    """Degraded-mode candidate set: the original filesystem walk, unchanged."""
    if not repo_root.is_dir():
        # Unreachable via main() (ROOT is derived from this file's location) —
        # a library caller with a nonexistent root gets "no violations", not a
        # traceback from iterdir().
        return []
    tests_root = repo_root / "backend" / "tests"
    violations: list[str] = []
    stack = [repo_root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir()):
            if entry.is_dir():
                if entry.name.startswith(".") or entry.name in PRUNE_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file() and _is_test_filename(entry.name):
                if tests_root == entry or tests_root in entry.parents:
                    continue
                violations.append(entry.relative_to(repo_root).as_posix())
    return violations


def find_violations(repo_root: Path) -> list[str]:
    """Return test files outside backend/tests/, as repo-relative POSIX paths."""
    tracked = _git_paths(repo_root, "ls-files", "-z")
    untracked = _git_paths(
        repo_root, "ls-files", "--others", "--exclude-standard", "-z"
    )
    if tracked is None or untracked is None:
        # Conservative on purpose: either call failing degrades the whole gate,
        # because a partial git view is not a safe candidate set. Each call is
        # bounded by GIT_TIMEOUT_SECONDS, so the degraded walk starts within
        # 2 x GIT_TIMEOUT_SECONDS of entry.
        print(DEGRADED_NOTE, file=sys.stderr)
        return _walk_violations(repo_root)
    violations: list[str] = []
    for rel in sorted(set(tracked) | set(untracked)):
        if not _is_test_filename(Path(rel).name):
            continue
        if not (repo_root / rel).is_file():
            continue
        if _has_hidden_parent(rel):
            continue
        if _is_under_backend_tests(rel):
            continue
        violations.append(rel)
    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    violations = find_violations(repo_root)
    if violations:
        print(
            "test-collection-scope: pytest test files found outside "
            "backend/tests/ (never collected by `pytest tests/`):",
            file=sys.stderr,
        )
        for path in violations:
            print(f"  {path}", file=sys.stderr)
        print(
            "Move them under backend/tests/ (as real, collectable tests) or "
            "delete them — see issue #563 / C11.",
            file=sys.stderr,
        )
        return 1
    print("test-collection-scope: OK (no test files outside backend/tests/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
