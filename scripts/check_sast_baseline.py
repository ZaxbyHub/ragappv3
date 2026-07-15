#!/usr/bin/env python3
"""SAST baseline integrity contract check.

Runs in CI (the Quality contracts job) alongside ``check_config_contract.py`` and
``check_pr_scope_drift.py``. It guards three integrity properties of the bandit
SAST gate that the gate itself cannot enforce:

1. **Workflow integrity (C-005):** ``.github/workflows/ci.yml`` must define a
   ``sast`` job whose step command runs ``scripts/run_bandit.py``. Without this,
   a PR could replace the scan command (e.g. ``echo pass``) and the gate would
   silently stop protecting anything.

2. **Scope integrity (C-003):** ``scripts/run_bandit.py``'s ``TARGET`` constant
   must equal the expected ``backend/app``. The scan scope is otherwise a single
   script-level constant a PR could silently shrink or redirect.

3. **Baseline-expansion gate (C-002):** when ``backend/security/bandit-baseline.json``
   is modified on a PR, the number of suppressed findings must NOT increase unless
   ``SAST_ALLOW_BASELINE_EXPANSION=1`` is set. Baseline growth suppresses findings —
   i.e. it weakens the gate — so it must be a deliberate, CI-visible act. When
   expansion is allowed, the newly-suppressed finding IDs are printed so a reviewer
   sees exactly what is being silenced. The initial creation of the baseline (no
   prior version on the merge-base) is permitted as seeding.

Exit non-zero on any violation. Run from the repository root.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_FILE = ROOT / ".github" / "workflows" / "ci.yml"
RUN_BANDIT = ROOT / "scripts" / "run_bandit.py"
BASELINE = ROOT / "backend" / "security" / "bandit-baseline.json"

EXPECTED_TARGET = "backend/app"
ALLOW_EXPANSION = os.environ.get("SAST_ALLOW_BASELINE_EXPANSION") == "1"


def fail(message: str) -> None:
    print(f"sast-baseline: {message}", file=sys.stderr)


def run_git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout


def changed_files() -> list[str]:
    """Changed files on this PR vs the merge-base with master (best-effort)."""
    for base in (
        os.environ.get("PR_SCOPE_DRIFT_BASE"),
        f"origin/{os.environ['GITHUB_BASE_REF']}" if os.environ.get("GITHUB_BASE_REF") else None,
        "origin/master",
    ):
        if not base:
            continue
        mb = run_git(["merge-base", base, "HEAD"]).strip()
        if not mb:
            continue
        diff = run_git(["diff", "--name-only", f"{mb}..HEAD"])
        if diff:
            return [line.strip() for line in diff.splitlines() if line.strip()]
    # Local fallback.
    diff = run_git(["diff", "--name-only", "HEAD~1..HEAD"])
    return [line.strip() for line in diff.splitlines() if line.strip()]


def check_workflow_integrity() -> bool:
    """C-005: the ci.yml must contain a sast job running run_bandit.py."""
    if not CI_FILE.exists():
        fail(f"{CI_FILE.relative_to(ROOT)} not found")
        return False
    text = CI_FILE.read_text(encoding="utf-8")
    has_sast_job = re.search(r"^  sast:\s*$", text, re.MULTILINE) is not None
    # Require a `run:` step whose command actually invokes the gated scan
    # (python ... run_bandit.py with NO --update-baseline). The token must appear
    # before any trailing `#` comment, and the command must not be the
    # always-exits-0 baseline-regeneration mode — otherwise a PR could swap the
    # gated scan for `run_bandit.py --update-baseline` and the gate would never
    # fail. Anchored to `run:` so a step name or prose reference cannot satisfy it.
    runs_run_bandit = False
    for m in re.finditer(r"^\s*run:\s*([^#\n]*)", text, re.MULTILINE):
        cmd = m.group(1)
        if "run_bandit.py" in cmd and "--update-baseline" not in cmd:
            runs_run_bandit = True
            break
    if not has_sast_job:
        fail("C-005: .github/workflows/ci.yml has no `sast:` job — SAST gate is missing")
        return False
    if not runs_run_bandit:
        fail(
            "C-005: ci.yml has no `run:` step invoking run_bandit.py — "
            "the gate command may have been replaced"
        )
        return False
    return True


def check_scope_integrity() -> bool:
    """C-003: run_bandit.py's TARGET must be the expected backend/app."""
    if not RUN_BANDIT.exists():
        fail(f"{RUN_BANDIT.relative_to(ROOT)} not found")
        return False
    text = RUN_BANDIT.read_text(encoding="utf-8")
    # Match the module-level TARGET assignment. Anchored to avoid matching
    # comments or substrings; the value is a quoted literal.
    m = re.search(r'^TARGET\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not m:
        fail(
            "C-003: could not find a module-level `TARGET = \"...\"` assignment in "
            "scripts/run_bandit.py — scan scope is not verifiable"
        )
        return False
    target = m.group(1)
    if target != EXPECTED_TARGET:
        fail(
            f"C-003: run_bandit.py TARGET is {target!r}, expected {EXPECTED_TARGET!r}. "
            "Shrinking or redirecting the SAST scope requires updating the expected "
            "value in scripts/check_sast_baseline.py deliberately."
        )
        return False
    return True


def _baseline_count(path_or_text: str | None, *, is_path: bool) -> int | None:
    if path_or_text is None:
        return None
    try:
        data = json.loads(path_or_text) if not is_path else json.loads(
            Path(path_or_text).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return len(data.get("results", []))


def _diff_added_keys(prev_text: str) -> list[str]:
    """Finding IDs present in the current baseline but absent from ``prev_text``."""
    try:
        prev_data = json.loads(prev_text)
        cur_data = json.loads(BASELINE.read_text(encoding="utf-8"))

        def _key(r: dict) -> str:
            fn = r.get("filename", "").replace("\\", "/")
            return f"{r.get('test_id', '?')} {fn}:{r.get('line_number', '?')}"

        prev_keys = {_key(r) for r in prev_data.get("results", [])}
        return sorted({_key(r) for r in cur_data.get("results", [])} - prev_keys)
    except (OSError, ValueError):
        return []


def check_baseline_expansion(paths: list[str]) -> bool:
    """C-002: baseline finding count must not grow on a PR without an explicit allow."""
    baseline_rel = "backend/security/bandit-baseline.json"

    # Fail-closed: if the baseline is locally modified but changed_files() could
    # not determine the PR diff (e.g. base resolution failed), we cannot verify
    # expansion — refuse to pass silently.
    baseline_locally_modified = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--", baseline_rel],
            cwd=ROOT, check=False, text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
    )
    if baseline_rel not in paths:
        if baseline_locally_modified:
            fail(
                f"C-002: {baseline_rel} is modified but the PR diff could not be "
                "determined (base ref unresolved); cannot verify baseline did not expand"
            )
            return False
        return True  # baseline untouched — nothing to gate

    if not BASELINE.exists():
        fail(f"{baseline_rel} is in the diff but absent from the working tree")
        return False

    # Find the prior version of the baseline on the merge-base / master. If there
    # is none, this is the initial seeding commit — allowed.
    prev_text = None
    for base in (
        os.environ.get("PR_SCOPE_DRIFT_BASE"),
        f"origin/{os.environ['GITHUB_BASE_REF']}" if os.environ.get("GITHUB_BASE_REF") else None,
        "origin/master",
    ):
        if not base:
            continue
        mb = run_git(["merge-base", base, "HEAD"]).strip()
        if not mb:
            continue
        show = subprocess.run(
            ["git", "show", f"{mb}:{baseline_rel}"],
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if show.returncode == 0:
            prev_text = show.stdout
            break

    current_count = _baseline_count(str(BASELINE), is_path=True)
    if current_count is None:
        fail(f"{baseline_rel} is not valid JSON — regenerate with run_bandit.py --update-baseline")
        return False

    if prev_text is None:
        # No prior baseline on the base branch → initial seeding. Allowed.
        print(
            f"sast-baseline: initial baseline seeded ({current_count} findings) "
            f"— C-002 expansion gate does not apply to creation"
        )
        return True

    prev_count = _baseline_count(prev_text, is_path=False)
    if prev_count is None:
        # Prior baseline existed but was corrupt — can't compare; fail safe.
        fail(
            f"prior {baseline_rel} on the base branch is not valid JSON; "
            "cannot verify baseline did not expand"
        )
        return False

    delta = current_count - prev_count
    added = _diff_added_keys(prev_text)

    if delta <= 0:
        # Did not grow. Surface any same-count finding-ID swaps so a reviewer
        # notices if a HIGH finding was silently traded for a different one.
        if added:
            print(
                f"sast-baseline: baseline {baseline_rel} did not grow "
                f"({prev_count} -> {current_count}) but finding IDs changed. "
                "Newly-suppressed IDs (review the swap):"
            )
            for key in added:
                print(f"  + {key}")
        else:
            print(
                f"sast-baseline: baseline {baseline_rel} changed but did not grow "
                f"({prev_count} -> {current_count}); OK"
            )
        return True

    # Baseline grew — suppresses new findings.
    if ALLOW_EXPANSION:
        print(
            f"sast-baseline: SAST_ALLOW_BASELINE_EXPANSION=1 — baseline grew by "
            f"{delta} ({prev_count} -> {current_count}). Newly-suppressed findings "
            "MUST be justified in the PR:"
        )
        for key in added:
            print(f"  + {key}")
        return True

    fail(
        f"C-002: {baseline_rel} grew by {delta} finding(s) ({prev_count} -> "
        f"{current_count}). Baseline expansion suppresses findings and weakens the "
        "gate. Either FIX the new findings, or — if they are acceptable pre-existing "
        "debt — re-run with SAST_ALLOW_BASELINE_EXPANSION=1 and justify each "
        "newly-suppressed ID in the PR description. Newly-suppressed findings:"
    )
    for key in added:
        print(f"  + {key}", file=sys.stderr)
    return False


def main() -> int:
    paths = changed_files()
    ok = True
    if not check_workflow_integrity():
        ok = False
    if not check_scope_integrity():
        ok = False
    if not check_baseline_expansion(paths):
        ok = False
    if ok:
        print("sast-baseline: all checks passed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
