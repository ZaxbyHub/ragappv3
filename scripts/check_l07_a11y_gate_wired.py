#!/usr/bin/env python3
"""Issue #778 L07 — the a11y route/viewport gate must actually be wired.

frontend/e2e/a11y-matrix.spec.ts is the FROZEN acceptance gate (axe matrix,
offender probe, probe self-test, main-landmark count). A gate that exists as
a file but is never run by CI protects nothing, and the existing `e2e` smoke
job runs a bare `npx playwright test` that would silently absorb it into the
smoke budget. This check wires the gate four ways:

  1. frontend/e2e/a11y-matrix.spec.ts exists (resolved against cwd, so the
     check runs from the repo root or any subdir; frozen issue-tracer
     worktrees resolve to their own root).
  2. .github/workflows/ci.yml has a job OTHER than the job named exactly
     `e2e` whose steps reference the literal `a11y-matrix.spec.ts`.
  3. The spec contains all four viewport literals — 320, 568, 640, 360 —
     as whole numbers (the VIEWPORTS definition: 320x568 and 640x360).
  4. The `e2e` job's Playwright run step does NOT implicitly pick up the
     a11y spec: its run line must name at least one *.spec.ts argument or
     use --grep-invert, instead of a bare `npx playwright test`.

Every miss prints one "MISSING a11y-matrix: <what>" line; any miss exits 1.
Success is silent and exits 0. A missing .github/workflows/ci.yml fails
closed with exit 2 (check_l05 missing-tree pattern). Pure line-based text
parsing — no YAML dependency (mirrors the other check_*.py ci.yml readers).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SPEC_REL = Path("frontend") / "e2e" / "a11y-matrix.spec.ts"
CI_REL = Path(".github") / "workflows" / "ci.yml"
E2E_JOB = "e2e"
SPEC_LITERAL = "a11y-matrix.spec.ts"
VIEWPORT_LITERALS = ("320", "568", "640", "360")

# A job key inside the GitHub Actions `jobs:` map: exactly two-space indent,
# name, colon, end of line (optional trailing comment).
JOB_HEADER_RE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$")


def resolve_repo_root() -> Path:
    """Repo root for path resolution: walk up from cwd to the git root; fall
    back to cwd (then the script's own parents[1]) when no .git is found."""
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    if (SPEC_REL).is_file() or (CI_REL).is_file():
        return cwd
    return Path(__file__).resolve().parents[1]


def split_jobs(ci_text: str) -> dict[str, list[str]]:
    """Split ci.yml into job blocks: {job name: lines inside the block}.

    Job keys are the two-space-indented `name:` lines that appear while
    inside the top-level `jobs:` map; any zero-indent line leaves it.
    """
    jobs: dict[str, list[str]] = {}
    in_jobs = False
    current: str | None = None
    for line in ci_text.splitlines():
        if line and not line[0].isspace():
            in_jobs = line.rstrip().endswith("jobs:")
            current = None
            continue
        if not in_jobs:
            continue
        match = JOB_HEADER_RE.match(line)
        if match:
            current = match.group(1)
            jobs.setdefault(current, [])
            continue
        if current is not None:
            jobs[current].append(line)
    return jobs


def main() -> int:
    root = resolve_repo_root()
    spec_path = root / SPEC_REL
    ci_path = root / CI_REL

    misses: list[str] = []

    # 1. The frozen spec exists.
    if not spec_path.is_file():
        misses.append(f"{SPEC_REL.as_posix()} not found (resolved: {spec_path})")

    # ci.yml is required for checks 2 and 4 — fail closed like check_l05's
    # missing scanned trees.
    if not ci_path.is_file():
        print(
            f"a11y-matrix: CI workflow not found (resolved: {ci_path})",
            file=sys.stderr,
        )
        return 2

    try:
        ci_text = ci_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"a11y-matrix: {exc}", file=sys.stderr)
        return 2

    jobs = split_jobs(ci_text)

    # 2. Some job OTHER than `e2e` references the spec literal.
    wired_jobs = [
        name
        for name, lines in jobs.items()
        if name != E2E_JOB and any(SPEC_LITERAL in ln for ln in lines)
    ]
    if not wired_jobs:
        misses.append(
            f"no CI job outside the '{E2E_JOB}' job references {SPEC_LITERAL} "
            f"in {CI_REL.as_posix()}"
        )

    # 3. The spec carries the four viewport literals (VIEWPORTS definition).
    if spec_path.is_file():
        try:
            spec_text = spec_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"a11y-matrix: {exc}", file=sys.stderr)
            return 2
        for literal in VIEWPORT_LITERALS:
            if not re.search(rf"\b{literal}\b", spec_text):
                misses.append(
                    f"viewport literal {literal} missing from {SPEC_REL.as_posix()}"
                )

    # 4. The `e2e` smoke step must not absorb new specs: every Playwright
    #    run line in the `e2e` job names at least one *.spec.ts argument or
    #    uses --grep-invert (a bare `npx playwright test` runs everything
    #    testDir picks up, the a11y matrix included).
    e2e_lines = jobs.get(E2E_JOB, [])
    if not e2e_lines:
        misses.append(f"job '{E2E_JOB}' not found in {CI_REL.as_posix()}")
    else:
        for line in e2e_lines:
            if "playwright test" not in line:
                continue
            if ".spec.ts" not in line and "--grep-invert" not in line:
                misses.append(
                    f"{E2E_JOB} smoke step runs a bare 'npx playwright test' "
                    f"and would absorb new specs"
                )

    if misses:
        for miss in misses:
            print(f"MISSING a11y-matrix: {miss}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
