#!/usr/bin/env python3
"""Issue #778 L07 — the a11y route/viewport gate must actually be wired.

frontend/e2e/a11y-matrix.spec.ts is the FROZEN acceptance gate (axe matrix,
offender probe, probe self-tests, main-landmark count). A gate that exists as
a file but is never run by CI protects nothing. Originally the `e2e` smoke
job ran a bare `npx playwright test` that would silently absorb it; that step
now names its four specs, and this check (strengthened by the issue #778
review feedback round) wires the gate:

  1. frontend/e2e/a11y-matrix.spec.ts exists (resolved against cwd, so the
     check runs from the repo root or any subdir; frozen issue-tracer
     worktrees resolve to their own root).
  2. .github/workflows/ci.yml has a job OTHER than the job named exactly
     `e2e` whose NON-COMMENT run line invokes playwright on
     a11y-matrix.spec.ts — and that job is not neutered by an `if:`-false
     condition or `continue-on-error: true`.
  3. The spec structurally pins both viewports (width: 320 / height: 568 /
     width: 640 / height: 360 — a comment mentioning the numbers does not
     count) and carries a roster of at least 20 routes (>= 20 `path: "`
     entries).
  4. The axe waiver cannot silently widen: the spec names exactly the four
     tracked-out rule families (#865 aria-valid-attr-value, #866
     nested-interactive, #867 button-name, #868 aria-required-children) and
     disables exactly four rules (`enabled: false` x 4).
  5. The `e2e` job's Playwright run lines name their own specs and never
     reference a11y-matrix.spec.ts; NO job may run playwright without naming
     at least one *.spec.ts, and the a11y-matrix literal may appear in only
     one job (the dedicated one) so the matrix cannot be re-absorbed.
  6. Census: every frontend/e2e/*.spec.ts file on disk is named by some
     non-comment playwright run line in ci.yml, so a new spec cannot run in
     no job at all.

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
E2E_DIR_REL = Path("frontend") / "e2e"
CI_REL = Path(".github") / "workflows" / "ci.yml"
E2E_JOB = "e2e"
SPEC_LITERAL = "a11y-matrix.spec.ts"
VIEWPORT_STRUCTURAL = (("320", "width"), ("568", "height"), ("640", "width"), ("360", "height"))
DISABLED_RULES = ("aria-valid-attr-value", "nested-interactive", "button-name", "aria-required-children")
MIN_ROUTES = 20

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
    Comment lines are skipped entirely (issue #778 review F-01/PRR-017): a
    job-leading comment must not be attributed to the preceding job, and a
    comment inside a job must never satisfy a literal match.
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
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = JOB_HEADER_RE.match(line)
        if match:
            current = match.group(1)
            jobs.setdefault(current, [])
            continue
        if current is not None:
            jobs[current].append(line)
    return jobs


def strip_inline_comment(line: str) -> str:
    """Drop a trailing `# comment` (conservative: only ' #' with whitespace,
    so `${{ ... }}` expressions and URL fragments survive untouched)."""
    idx = line.find(" #")
    return line[:idx] if idx != -1 else line


def run_lines(job_lines: list[str]) -> list[str]:
    """Non-comment lines that invoke playwright (comment-stripped)."""
    return [
        strip_inline_comment(ln)
        for ln in job_lines
        if "playwright test" in strip_inline_comment(ln)
    ]


def main() -> int:
    root = resolve_repo_root()
    spec_path = root / SPEC_REL
    ci_path = root / CI_REL

    misses: list[str] = []

    # 1. The frozen spec exists.
    if not spec_path.is_file():
        misses.append(f"{SPEC_REL.as_posix()} not found (resolved: {spec_path})")

    # ci.yml is required for the wiring checks — fail closed like check_l05's
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

    # 2. A dedicated job (not `e2e`) invokes playwright on the spec from a
    #    non-comment run line, and is not neutered by if:-false or
    #    continue-on-error (issue #778 review F-01 — GitHub reports a job
    #    skipped by `if: false` as success).
    wired_jobs = [
        name
        for name, lines in jobs.items()
        if name != E2E_JOB
        and any(
            "playwright test" in ln and SPEC_LITERAL in ln
            for ln in run_lines(lines)
        )
    ]
    if not wired_jobs:
        misses.append(
            f"no CI job outside the '{E2E_JOB}' job runs {SPEC_LITERAL} "
            f"in {CI_REL.as_posix()}"
        )
    else:
        for name in wired_jobs:
            joined = "\n".join(jobs[name])
            if re.search(r"^\s*if:.*\bfalse\b", joined, re.MULTILINE):
                misses.append(f"the '{name}' job carries an if:-false condition")
            if re.search(r"^\s*continue-on-error:\s*true", joined, re.MULTILINE):
                misses.append(f"the '{name}' job sets continue-on-error: true")

    # Structural spec checks (only meaningful when the spec exists).
    spec_lines: list[str] = []
    if spec_path.is_file():
        try:
            spec_text = spec_path.read_text(encoding="utf-8")
            spec_lines = spec_text.splitlines()
        except OSError as exc:
            print(f"a11y-matrix: {exc}", file=sys.stderr)
            return 2

        # 3a. Structural viewport pins: a comment mentioning "640" no longer
        #     satisfies the check (issue #778 review F-06) — the VIEWPORTS
        #     entries must carry width/height literals.
        for literal, axis in VIEWPORT_STRUCTURAL:
            if not re.search(rf"{axis}:\s*{literal}\b", spec_text):
                misses.append(
                    f"viewport literal {axis}: {literal} missing from "
                    f"{SPEC_REL.as_posix()}"
                )

        # 3b. Roster pin: the gate must keep >= MIN_ROUTES routes
        #     (issue #778 review PRR-006).
        roster = sum(1 for ln in spec_lines if re.match(r'\s*path: "', ln))
        if roster < MIN_ROUTES:
            misses.append(
                f"route roster has {roster} entries, below the required "
                f"{MIN_ROUTES} (>= 20 gated routes)"
            )

        # 4. The axe waiver cannot silently widen: exactly the four tracked
        #    rule ids, exactly four disables (issue #778 review PRR-006).
        for rule in DISABLED_RULES:
            if f'"{rule}"' not in spec_text:
                misses.append(f"disabled rule '{rule}' missing from the spec")
        disables = spec_text.count("enabled: false")
        if disables != len(DISABLED_RULES):
            misses.append(
                f"the spec disables {disables} axe rules; exactly "
                f"{len(DISABLED_RULES)} are sanctioned (the #865-#868 set)"
            )

    # 5. Run-line hygiene across ALL jobs (issue #778 review PRR-016):
    #    every playwright invocation names at least one *.spec.ts; the e2e
    #    job must not reference the a11y spec; the a11y literal appears in
    #    exactly one job (the dedicated one) so the matrix cannot be
    #    re-absorbed into the smoke budget.
    literal_jobs: set[str] = set()
    for name, lines in jobs.items():
        for ln in run_lines(lines):
            if SPEC_LITERAL in ln:
                literal_jobs.add(name)
            if ".spec.ts" not in ln:
                misses.append(
                    f"job '{name}' runs playwright without naming a spec file"
                )
    if E2E_JOB in literal_jobs:
        misses.append(
            f"the '{E2E_JOB}' job references {SPEC_LITERAL}; the matrix must "
            "run in its own job"
        )
    if len(literal_jobs) > 1:
        misses.append(
            f"{SPEC_LITERAL} appears in {len(literal_jobs)} jobs "
            f"({', '.join(sorted(literal_jobs))}); it must run in exactly one"
        )

    # 6. Census (issue #778 review F-07): every spec file on disk is named by
    #    some run line, so a new spec cannot silently run in no job at all.
    e2e_dir = root / E2E_DIR_REL
    if e2e_dir.is_dir():
        on_disk = sorted(p.name for p in e2e_dir.glob("*.spec.ts"))
        all_run_lines = [ln for lines in jobs.values() for ln in run_lines(lines)]
        for name in on_disk:
            if not any(name in ln for ln in all_run_lines):
                misses.append(
                    f"spec file {name} is not named by any playwright run "
                    f"line in {CI_REL.as_posix()}"
                )

    if misses:
        for miss in misses:
            print(f"MISSING a11y-matrix: {miss}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
