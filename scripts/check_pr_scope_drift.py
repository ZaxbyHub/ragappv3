#!/usr/bin/env python3
"""Flag high-risk test expectation drift without matching implementation changes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def changed_files() -> list[str]:
    explicit_base = os.environ.get("PR_SCOPE_DRIFT_BASE")
    github_base = os.environ.get("GITHUB_BASE_REF")
    candidates = []
    if explicit_base:
        candidates.append(explicit_base)
    if github_base:
        candidates.append(f"origin/{github_base}")
    candidates.append("origin/master")

    for base in candidates:
        merge_base = run_git(["merge-base", base, "HEAD"])
        if merge_base.returncode != 0:
            continue
        diff = run_git(["diff", "--name-only", f"{merge_base.stdout.strip()}..HEAD"])
        if diff.returncode == 0:
            return [line.strip() for line in diff.stdout.splitlines() if line.strip()]

    fallback = run_git(["diff", "--name-only", "HEAD~1..HEAD"])
    if fallback.returncode == 0:
        return [line.strip() for line in fallback.stdout.splitlines() if line.strip()]
    print(
        "scope-drift: unable to determine changed files; skipping drift check",
        file=sys.stderr,
    )
    return []


def has_path(paths: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefixes) for path in paths)


def main() -> int:
    paths = changed_files()
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
            "backend/app/services/auth",
            "backend/app/models/auth",
            "backend/app/deps.py",
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
            "scripts/check_skill_sync.py",
            "scripts/check_secretscan.py",
            "scripts/sync_skills.py",
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
