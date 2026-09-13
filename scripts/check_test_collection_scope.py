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

Hidden directories (``.git``, ``.venv*``, ``.agents/issue-traces``, ...),
``node_modules``, ``__pycache__``, and the non-hidden virtualenv names
``venv``/``env``/``ENV`` (INSTALLATION.md's ``python -m venv venv`` is
non-hidden and ships hundreds of packaged test files inside site-packages)
are pruned from the walk: vendored, generated, virtualenv, and
agent-artifact trees are not pytest collection surfaces.

Exit codes: 0 = clean, 1 = violations found (each offender printed).
Run from the repository root.
"""

import sys
from pathlib import Path

PRUNE_DIRS = {"__pycache__", "node_modules", "venv", "env", "ENV"}
TEST_FILE_SUFFIXES = (".py",)


def _is_test_filename(name: str) -> bool:
    return (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(
        TEST_FILE_SUFFIXES
    )


def find_violations(repo_root: Path) -> list[str]:
    """Return test files outside backend/tests/, as repo-relative POSIX paths."""
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
