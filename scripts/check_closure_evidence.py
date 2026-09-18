#!/usr/bin/env python3
"""Closure-evidence gate for pull requests that auto-close issues (#568, E13).

A PR whose body says ``Closes #N`` / ``Fixes #N`` may only carry that claim
when it names closure evidence the gate can check: a backend pytest test id
that FAILS on the pre-fix commit and PASSES on the PR head (re-executed by
``verify-test``), or — only when no executable test id is named — a captured
artifact that actually exists in the repository. Issues labeled ``high`` or
``critical`` additionally require an approving review from outside the fix's
own file family. The convention enforced here is the repo's existing prose
standard (.opencode/skills/issue-tracer/SKILL.md: "Regression test fails
before the fix and passes after the fix when feasible";
.opencode/skills/swarm-implement/SKILL.md: "Regression tests must be
falsifiable"); this gate adds the missing enforcement, it does not redefine
the standard.

Enforcement model: GitHub closes the issue at MERGE as a side effect of the
closing keyword — nothing can block that event itself. Enforcement is a
required failing check on the PR (enforce mode) plus the documented
reopen/comment-on-merge procedure in docs/ci/closure-evidence-gate.md.
Rollout ships warn-only; the flip to enforce is the workflow's ``mode``
input. Rollback is disabling or deleting .github/workflows/closure-evidence.yml.

Subcommands:
  evaluate      structural gate over a PR snapshot (offline --pr-data, or
                live --pr N --repo-slug O/R via ``gh api``)
  verify-test   re-execute a named test at a base and head commit and assert
                fail-at-base / pass-at-head

Output lines are prefixed ``closure-evidence:`` (OK / WARN / FAIL / ERROR /
evidence-test). The MODE governs the exit code only; diagnostics are always
truthful: warn mode never exits non-zero (rollout contract), enforce mode
exits 1 on any violation or live-fetch failure.

Standard library only (Python 3.11). Run from the repository root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# GitHub's closing-keyword set (same family zaxbygraph indexes on).
CLOSE_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*#\s*(\d+)",
    re.IGNORECASE,
)

# Executable evidence: a backend pytest file or node id.
TEST_ID_RE = re.compile(r"backend/tests/[A-Za-z0-9_/.-]+\.py(?:::[A-Za-z0-9_]+)*")

# Artifact evidence: ``artifact: <repo-relative-path>``. URL-form artifacts
# are advisory only — the gate never makes network calls to validate them.
ARTIFACT_RE = re.compile(r"\bartifact[:=]\s*([^\s,;]+)", re.IGNORECASE)

HIGH_LABELS = {"high", "critical"}

# File-family mapping: explicit prefix table (documented in
# docs/ci/closure-evidence-gate.md). Longest prefix wins.
FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("backend/app/api", "backend-api"),
    ("backend/app/services", "backend-services"),
    ("backend/app/models", "backend-models"),
    ("backend/app", "backend-core"),
    ("backend/tests", "backend-tests"),
    ("frontend/src", "frontend-src"),
    ("scripts/", "tooling-scripts"),
    (".github/", "ci-workflows"),
    ("docs/", "docs"),
)

RUN_TIMEOUT_SECONDS = 240
GH_TRANSIENT_RETRIES = 1
MAX_COMMITS_WITH_FILES = 100


class FetchError(Exception):
    """Live fetch failed in a way that is not an issue-404 skip."""


def family_of(path: str) -> str:
    for prefix, family in FAMILY_PREFIXES:
        if path.startswith(prefix):
            return family
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 1 else path


def extract_close_refs(body: str | None) -> list[int]:
    if not body:
        return []
    seen: dict[int, None] = {}
    for match in CLOSE_REF_RE.finditer(body):
        seen.setdefault(int(match.group(1)), None)
    return list(seen)


def extract_evidence(body: str | None) -> tuple[str | None, str | None, list[str]]:
    """Return (first_test_id, artifact_path_or_None, additional_test_ids)."""
    if not body:
        return None, None, []
    test_ids = TEST_ID_RE.findall(body)
    artifact_match = ARTIFACT_RE.search(body)
    artifact = artifact_match.group(1).rstrip(".") if artifact_match else None
    first, *extra = test_ids if test_ids else (None, [])
    return first, artifact, list(extra)


def evaluate_snapshot(snapshot: dict, mode: str, repo_root: Path) -> tuple[list[str], str | None]:
    """Evaluate an offline PR snapshot. Returns (violations, evidence_test_id)."""
    body = snapshot.get("body") or ""
    changed_files = list(snapshot.get("changed_files") or [])
    commits = list(snapshot.get("commits") or [])
    reviews = list(snapshot.get("reviews") or [])
    issue_labels = snapshot.get("issue_labels") or {}

    refs = extract_close_refs(body)
    if not refs:
        return [], None

    violations: list[str] = []
    test_id, artifact, _extra = extract_evidence(body)

    if test_id is None:
        if artifact is None:
            violations.append(
                f"no closure evidence named for issue(s) #{', #'.join(str(r) for r in refs)} "
                "— name a backend pytest test id (backend/tests/...py::test) or an "
                "existing captured artifact (artifact: <repo-relative-path>)"
            )
        else:
            if not (repo_root / artifact).is_file():
                violations.append(
                    f"closure evidence artifact named but missing from the "
                    f"repository: {artifact} — a named-but-absent artifact is "
                    "exactly the unverified closure claim this gate exists to catch"
                )

    high_refs = [r for r in refs if HIGH_LABELS & {str(x).lower() for x in (issue_labels.get(str(r)) or [])}]
    if high_refs:
        pr_families = {family_of(p) for p in changed_files}
        same_family_logins: set[str] = set()
        for commit in commits:
            author = str(commit.get("author") or "")
            files = list(commit.get("files") or [])
            if any(family_of(f) in pr_families for f in files):
                same_family_logins.add(author)
        cross_family = [
            review
            for review in reviews
            if str(review.get("state") or "").upper() == "APPROVED"
            and str(review.get("author") or "") not in same_family_logins
            and not str(review.get("author") or "").endswith("[bot]")
        ]
        if not cross_family:
            violations.append(
                f"issue(s) #{', #'.join(str(r) for r in high_refs)} labeled "
                f"{'/'.join(sorted(HIGH_LABELS & {str(x).lower() for r in high_refs for x in (issue_labels.get(str(r)) or [])}))} "
                "require an approving review from outside the fix's own file "
                f"family ({', '.join(sorted(pr_families))}); every approving "
                "reviewer authored commits touching those families (or is a bot)"
            )

    return violations, test_id


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GH_TOKEN", env.get("GITHUB_TOKEN", ""))
    return subprocess.run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )


def gh_api(endpoint: str) -> dict | list:
    """One ``gh api`` call with a single retry on transient failures."""
    last_error = ""
    for attempt in range(1 + GH_TRANSIENT_RETRIES):
        try:
            proc = _gh([endpoint])
        except FileNotFoundError as exc:
            raise FetchError(f"gh CLI not available: {exc}") from exc
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise FetchError(f"gh api returned unparseable JSON for {endpoint}: {exc}") from exc
        stderr = proc.stderr or ""
        if "Not Found" in stderr or "not found" in stderr.lower():
            raise LookupError(endpoint)
        last_error = stderr.strip() or f"exit {proc.returncode}"
        if attempt < GH_TRANSIENT_RETRIES:
            time.sleep(1.5)
    raise FetchError(f"gh api {endpoint} failed: {last_error}")


def fetch_snapshot(pr_number: int, repo_slug: str) -> dict:
    """Assemble the offline snapshot shape from the live GitHub API."""
    try:
        pull = gh_api(f"repos/{repo_slug}/pulls/{pr_number}")
        files = gh_api(f"repos/{repo_slug}/pulls/{pr_number}/files?per_page=100")
        commit_list = gh_api(f"repos/{repo_slug}/pulls/{pr_number}/commits?per_page={MAX_COMMITS_WITH_FILES}")
        reviews = gh_api(f"repos/{repo_slug}/pulls/{pr_number}/reviews?per_page=100")
    except LookupError:
        raise FetchError(f"pull request #{pr_number} not found in {repo_slug}") from None

    body_refs = extract_close_refs(pull.get("body") or "")
    issue_labels: dict[str, list[str]] = {}
    for ref in body_refs:
        try:
            issue = gh_api(f"repos/{repo_slug}/issues/{ref}")
        except LookupError:
            continue  # issue gone; the ref is skipped
        issue_labels[str(ref)] = [label.get("name", "") for label in issue.get("labels") or []]

    commits: list[dict] = []
    for entry in commit_list:
        author = (entry.get("author") or {}).get("login") or (entry.get("commit") or {}).get("author", {}).get("name", "")
        try:
            detail = gh_api(entry["url"])
            commit_files = [f.get("filename", "") for f in detail.get("files") or []]
        except (FetchError, LookupError, KeyError):
            commit_files = []
        commits.append({"author": author, "files": commit_files})

    return {
        "body": pull.get("body") or "",
        "changed_files": [f.get("filename", "") for f in files],
        "commits": commits,
        "reviews": [
            {"author": (r.get("user") or {}).get("login", ""), "state": r.get("state", "")}
            for r in reviews
        ],
        "issue_labels": issue_labels,
    }


def cmd_evaluate(args: argparse.Namespace) -> int:
    mode = args.mode
    snapshot: dict
    if args.pr_data:
        try:
            snapshot = json.loads(Path(args.pr_data).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"closure-evidence: ERROR — cannot read PR snapshot {args.pr_data}: {exc}")
            return 1 if mode == "enforce" else 0
    else:
        try:
            snapshot = fetch_snapshot(args.pr, args.repo_slug)
        except FetchError as exc:
            print(f"closure-evidence: ERROR — live fetch failed: {exc}")
            return 1 if mode == "enforce" else 0

    violations, test_id = evaluate_snapshot(snapshot, mode, ROOT)
    refs = extract_close_refs(snapshot.get("body") or "")

    if refs:
        print(
            f"closure-evidence: evaluating PR close refs "
            f"#{', #'.join(str(r) for r in refs)} in {mode} mode"
        )
    if test_id and refs:
        print(f"closure-evidence: evidence-test {test_id}")

    if not violations:
        if refs:
            print("closure-evidence: OK — closure evidence satisfied")
        else:
            print("closure-evidence: OK — no closing reference in PR body; gate not applicable")
        return 0

    for violation in violations:
        marker = "FAIL" if mode == "enforce" else "WARN"
        print(f"closure-evidence: {marker} — {violation}")
    if mode == "enforce":
        print(
            "closure-evidence: blocked in enforce mode; GitHub cannot block the "
            "merge-time auto-close itself — see docs/ci/closure-evidence-gate.md "
            "for the reopen-on-merge procedure"
        )
        return 1
    return 0


UNITTEST_DRIVER = (
    "import os, sys, unittest\n"
    "sys.path.insert(0, os.getcwd())\n"
    "loader = unittest.defaultTestLoader\n"
    "suite = loader.loadTestsFromName(sys.argv[1])\n"
    "runner = unittest.TextTestRunner(verbosity=0)\n"
    "result = runner.run(suite)\n"
    "sys.exit(0 if result.wasSuccessful() else 1)\n"
)


def _pytest_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("pytest") is not None
    except (ImportError, ValueError):
        return False


def _module_for(test_path: str) -> str:
    return test_path[:-3].replace("/", ".").replace("\\", ".")


def run_test_once(worktree: Path, test_id: str) -> tuple[str, str]:
    """Run the named test once inside a worktree. Returns (verdict, detail).

    Verdict is PASS, NOT_PASS, or ERROR. NOT_PASS covers both genuine test
    failures and collection errors (a test that cannot run at a commit is not
    passing there — for the base side that includes "the test did not exist
    yet", which is exactly the pre-fix state the gate wants to see).
    """
    use_pytest = "::" in test_id or _pytest_available()
    if "::" in test_id and not _pytest_available():
        return "ERROR", (
            f"node id {test_id} requires pytest, which is not importable in "
            "this interpreter (fail-closed)"
        )
    if use_pytest:
        cmd = [sys.executable, "-m", "pytest", test_id, "-q"]
    else:
        cmd = [sys.executable, "-I", "-c", UNITTEST_DRIVER, _module_for(test_id)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "ERROR", f"runner timed out after {RUN_TIMEOUT_SECONDS}s"
    tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-6:])
    if use_pytest:
        if proc.returncode == 0:
            return "PASS", tail
        if proc.returncode in (1, 2, 4, 5):
            return "NOT_PASS", tail
        return "ERROR", tail
    if proc.returncode == 0:
        return "PASS", tail
    if "Traceback" in (proc.stderr or "") and "loadTestsFromName" in (proc.stderr or ""):
        return "ERROR", tail
    return "NOT_PASS", tail


def cmd_verify_test(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    base, head = args.base, args.head

    worktrees: list[Path] = []
    tmp_root = Path(tempfile.mkdtemp(prefix="closure-evidence-verify-"))
    try:
        sides: dict[str, tuple[str, str]] = {}
        for label, sha in (("base", base), ("head", head)):
            worktree = tmp_root / label
            proc = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), sha],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if proc.returncode != 0:
                print(
                    f"closure-evidence: FAIL — cannot check out {label} commit "
                    f"{sha}: {(proc.stderr or '').strip()}"
                )
                return 1
            worktrees.append(worktree)
            sides[label] = run_test_once(worktree, args.test)

        base_verdict, base_detail = sides["base"]
        head_verdict, head_detail = sides["head"]
        print(f"closure-evidence: verify-test base {base} -> {base_verdict}")
        print(f"closure-evidence: verify-test head {head} -> {head_verdict}")

        if base_verdict == "PASS":
            print(
                "closure-evidence: FAIL — the evidence test PASSED at the "
                "base/pre-fix commit, so the PR fixes nothing this test can "
                "detect (the #343 pattern: a closure claim without a falsifiable "
                "pre-fix failure)"
            )
            return 1
        if base_verdict == "ERROR":
            print(f"closure-evidence: FAIL — runner error at base: {base_detail}")
            return 1
        if head_verdict == "PASS":
            print(
                "closure-evidence: OK — evidence test fails at base (pre-fix) "
                "and passes at head"
            )
            return 0
        if head_verdict == "ERROR":
            print(f"closure-evidence: FAIL — runner error at head: {head_detail}")
            return 1
        print(f"closure-evidence: FAIL — the evidence test does not pass at head: {head_detail}")
        return 1
    finally:
        for worktree in worktrees:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                capture_output=True,
                check=False,
            )
        shutil.rmtree(tmp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    evaluate = sub.add_parser("evaluate", help="structural closure-evidence gate over a PR snapshot")
    source = evaluate.add_mutually_exclusive_group(required=True)
    source.add_argument("--pr-data", help="offline PR snapshot JSON file")
    source.add_argument("--pr", type=int, help="live PR number (uses gh api)")
    evaluate.add_argument("--repo-slug", default="ZaxbyHub/ragappv3", help="owner/repo for live mode")
    evaluate.add_argument("--mode", choices=("warn", "enforce"), default="warn")
    evaluate.set_defaults(func=cmd_evaluate)

    verify = sub.add_parser("verify-test", help="re-execute a named test at base and head")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--base", required=True)
    verify.add_argument("--head", required=True)
    verify.add_argument("--test", required=True)
    verify.set_defaults(func=cmd_verify_test)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
