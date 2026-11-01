#!/usr/bin/env python3
"""Flag high-risk test expectation drift without matching implementation changes."""

from __future__ import annotations

import importlib.util as _ilu
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Shared hardened git plumbing (issue #740): bounded, OSError-tolerant,
# NUL-delimited listings. Loaded file-relative so the module resolves
# identically under `python scripts/check_pr_scope_drift.py` and under test
# loaders that spec-load this script without scripts/ on sys.path.
_gate_git_spec = _ilu.spec_from_file_location(
    "_gate_git", Path(__file__).resolve().parent / "_gate_git.py"
)
_gate_git = _ilu.module_from_spec(_gate_git_spec)
_gate_git_spec.loader.exec_module(_gate_git)


def changed_files() -> list[str] | None:
    """Changed files vs the merge-base, or None when git cannot answer at
    all (binary missing / timeout / OSError — distinct from a legitimate
    empty answer). The caller fails closed under GITHUB_ACTIONS (PR #1045
    review, PRR-008/F2-01: this gate used to print "all checks passed" on
    exactly that fault)."""
    explicit_base = os.environ.get("PR_SCOPE_DRIFT_BASE")
    github_base = os.environ.get("GITHUB_BASE_REF")
    candidates = []
    if explicit_base:
        candidates.append(explicit_base)
    if github_base:
        candidates.append(f"origin/{github_base}")
    candidates.append("origin/master")

    for base in candidates:
        # NUL-delimited listings (issue #740): a non-ASCII tracked path
        # arrives raw instead of core.quotePath C-quoted, so it can never
        # silently evade the prefix checks below.
        merge_base = _gate_git.git_lines(ROOT, "merge-base", base, "HEAD")
        if merge_base is None:
            return None
        if not merge_base:
            continue
        diff = _gate_git.git_lines(
            ROOT, "diff", "--name-only", f"{merge_base[0]}..HEAD"
        )
        if diff is None:
            return None
        if diff:
            return diff
        return []

    fallback = _gate_git.git_lines(ROOT, "diff", "--name-only", "HEAD~1..HEAD")
    if fallback is None:
        return None
    return fallback


def has_path(paths: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefixes) for path in paths)


def main() -> int:
    paths = changed_files()
    if paths is None:
        # git could not answer at all: fail closed in CI (a gate must not
        # print "all checks passed" on an instrument fault — PR #1045
        # review, PRR-008), degrade with the skip note locally.
        message = "unable to determine changed files (git could not answer); drift check cannot run"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"scope-drift: {message}", file=sys.stderr)
            return 1
        print(f"scope-drift: {message}", file=sys.stderr)
        return 0
    failures: list[str] = []

    auth_test_changed = any(
        path.startswith("backend/tests/test_auth") and path.endswith(".py")
        for path in paths
    )
    auth_runtime_changed = has_path(
        paths,
        (
            "backend/app/api/routes/auth.py",
            "backend/app/security.py",
            # Prefix, not directory: also covers auth_service.py and
            # authz_policy.py (issue #740 — the two dead entries below were
            # removed because no repository path starts with them; the real
            # dependency module joins the list).
            "backend/app/services/auth",
            "backend/app/api/deps.py",
        ),
    )
    auth_docs_changed = has_path(paths, ("CHANGELOG.md", "docs/", "INSTALLATION.md"))
    if auth_test_changed and not (auth_runtime_changed or auth_docs_changed):
        failures.append(
            "auth tests changed without matching auth runtime or documented policy changes"
        )

    ci_changed = has_path(paths, (".github/workflows/", "frontend/package", "backend/requirements"))
    ci_contract_changed = has_path(
        paths,
        (
            "scripts/check_config_contract.py",
            "scripts/check_pr_scope_drift.py",
            "scripts/check_closure_evidence.py",
            "scripts/check_secretscan.py",
            "scripts/check_test_collection_scope.py",
            ".agents/skills/ci-compatibility-audit/",
        ),
    )
    if ci_changed and not ci_contract_changed:
        print(
            "scope-drift: CI/tooling changed; verify local commands match "
            ".github/workflows/ci.yml",
            file=sys.stderr,
        )

    for message in failures:
        print(f"scope-drift: {message}", file=sys.stderr)
    if failures:
        return 1
    print("scope-drift: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
