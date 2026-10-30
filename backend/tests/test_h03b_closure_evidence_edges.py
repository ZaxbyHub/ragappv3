"""Edge coverage beyond the frozen #741 checks (unfrozen, free to iterate).

The frozen acceptance file (test_h03_closure_evidence.py) pins the four
mechanical defects; this file adds the boundary and adversarial shapes the
plan critic demanded for the revised design:

* the GitHub commits endpoint caps at 250 — a NON-page-multiple (pages of
  100+100+50 end SHORT), so page-size loops alone accept a truncated list;
  completeness must fail closed against the pull request's own ``commits``
  count field;
* review history arrives paginated — a superseding CHANGES_REQUESTED beyond
  page one must still neutralize a stale approval;
* DISMISSED-as-latest, COMMENTED-only histories, and empty logins can never
  satisfy the cross-family rule;
* the workflow runs the base checker under ``python -I`` — a module planted
  beside the script must not shadow stdlib imports;
* the workflow's Evaluate preflight is status-captured: a failing ``gh`` or
  ``git checkout`` records a truthful ERROR (eval_rc=1) without running the
  checker, keeping warn mode non-blocking.

All green at the fixed tree by construction (these shapes do not exist at
base). No network: the GitHub layer is monkeypatched, and the workflow legs
run against PATH-shimmed binaries.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_closure_evidence as cce  # noqa: E402

WORKFLOW_YML = REPO_ROOT / ".github" / "workflows" / "closure-evidence.yml"


def _commit_entry(i: int) -> dict:
    return {
        "author": {"login": f"committer{i}"},
        "commit": {"author": {"name": f"Committer {i}"}},
        "url": f"https://api.github.com/repos/ZaxbyHub/ragappv3/commits/c{i:04d}",
    }


class _GhPages:
    """Serve paginated lists plus a pull payload with authoritative counts.

    ``cap`` truncates a list the way GitHub's endpoints do (short final page
    at exactly ``cap`` items), which is the shape page-size loops mis-accept.
    """

    def __init__(self, *, commits: list, files: list, reviews: list, pull: dict):
        self.commits = commits
        self.files = files
        self.reviews = reviews
        self.pull = pull
        self.review_pages_served: list[int] = []

    def __call__(self, args: list[str]) -> SimpleNamespace:
        endpoint = args[0]
        per_match = re.search(r"per_page=(\d+)", endpoint)
        per_page = int(per_match.group(1)) if per_match else 100
        page_match = re.search(r"[?&]page=(\d+)", endpoint)
        page = int(page_match.group(1)) if page_match else 1
        if endpoint.startswith("http"):
            payload: object = {"files": [{"filename": "backend/app/services/x.py"}]}
        elif "/reviews?" in endpoint:
            self.review_pages_served.append(page)
            payload = self.reviews[(page - 1) * per_page : page * per_page]
        elif "/commits?" in endpoint:
            payload = self.commits[(page - 1) * per_page : page * per_page]
        elif "/files?" in endpoint:
            payload = self.files[(page - 1) * per_page : page * per_page]
        elif "/issues/" in endpoint:
            payload = {"labels": []}
        elif "/pulls/" in endpoint:
            payload = self.pull
        else:  # pragma: no cover - unknown endpoint must fail loudly
            return SimpleNamespace(returncode=0, stdout="not-json", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def _install(monkeypatch, gh: _GhPages) -> None:
    monkeypatch.setattr(cce, "_gh", gh)


def _pull(body: str = "Closes #5", **counts: int) -> dict:
    payload: dict = {"body": body}
    payload.update(counts)
    return payload


def test_commit_cap_at_250_fails_closed_on_count_mismatch(monkeypatch):
    # GitHub returns at most 250 commits (100+100+50 — a SHORT final page, so
    # a page-size loop alone would accept it). The PR's own count field (300)
    # must drive a FetchError instead of a silently truncated snapshot.
    gh = _GhPages(
        commits=[_commit_entry(i) for i in range(300)][:250],
        files=[{"filename": "backend/app/services/f.py"}],
        reviews=[],
        pull=_pull(commits=300, changed_files=1),
    )
    _install(monkeypatch, gh)
    with pytest.raises(cce.FetchError, match="truncated"):
        cce.fetch_snapshot(1, "ZaxbyHub/ragappv3")


def test_commit_count_match_passes(monkeypatch):
    # 250 fetched, PR reports exactly 250: complete, no FetchError.
    gh = _GhPages(
        commits=[_commit_entry(i) for i in range(250)],
        files=[{"filename": "backend/app/services/f.py"}],
        reviews=[],
        pull=_pull(commits=250, changed_files=1),
    )
    _install(monkeypatch, gh)
    snapshot = cce.fetch_snapshot(1, "ZaxbyHub/ragappv3")
    assert len(snapshot["commits"]) == 250


def test_file_count_mismatch_fails_closed(monkeypatch):
    gh = _GhPages(
        commits=[_commit_entry(0)],
        files=[{"filename": f"backend/app/services/f{i:03d}.py"} for i in range(100)],
        reviews=[],
        pull=_pull(commits=1, changed_files=140),
    )
    _install(monkeypatch, gh)
    with pytest.raises(cce.FetchError, match="truncated"):
        cce.fetch_snapshot(1, "ZaxbyHub/ragappv3")


def test_superseding_review_beyond_page_one_neutralizes_approval(monkeypatch):
    # 102 reviews: the sole committer's approvals fill page one (same-family,
    # never cross-family candidates); an outsider's APPROVED and their later
    # CHANGES_REQUESTED both live on page two. The paginated fetch must see
    # the supersession, and the decided rule must refuse the stale approval
    # as cross-family evidence.
    reviews: list[dict] = [
        {"user": {"login": "insider0"}, "state": "APPROVED"} for _ in range(100)
    ]
    reviews.append({"user": {"login": "outsider"}, "state": "APPROVED"})
    reviews.append({"user": {"login": "outsider"}, "state": "CHANGES_REQUESTED"})
    gh = _GhPages(
        commits=[{"author": {"login": "insider0"}, "commit": {"author": {"name": "I"}}, "url": "https://api.github.com/repos/ZaxbyHub/ragappv3/commits/c1"}],
        files=[{"filename": "backend/app/services/foo.py"}],
        reviews=reviews,
        pull=_pull(commits=1, changed_files=1),
    )
    _install(monkeypatch, gh)
    snapshot = cce.fetch_snapshot(1, "ZaxbyHub/ragappv3")
    assert 2 in gh.review_pages_served, "pagination never reached the superseding review"
    snapshot["issue_labels"] = {"5": ["high"]}
    violations, _ = cce.evaluate_snapshot(snapshot, "enforce", REPO_ROOT)
    assert any("cross-family" in v or "outside the fix" in v for v in violations), violations


def test_dismissed_as_latest_does_not_count(tmp_path):
    snapshot = {
        "body": "Closes #5",
        "changed_files": ["backend/app/services/foo.py"],
        "commits": [{"author": "insider", "files": ["backend/app/services/foo.py"]}],
        "reviews": [
            {"author": "insider", "state": "APPROVED"},
            {"author": "approver", "state": "APPROVED"},
            {"author": "approver", "state": "DISMISSED"},
        ],
        "issue_labels": {"5": ["high"]},
    }
    violations, _ = cce.evaluate_snapshot(snapshot, "enforce", tmp_path)
    assert any("cross-family" in v or "outside the fix" in v for v in violations), violations


def test_commented_only_history_never_counts(tmp_path):
    snapshot = {
        "body": "Closes #5",
        "changed_files": ["backend/app/services/foo.py"],
        "commits": [{"author": "insider", "files": ["backend/app/services/foo.py"]}],
        "reviews": [{"author": "chatty", "state": "COMMENTED"}],
        "issue_labels": {"5": ["high"]},
    }
    violations, _ = cce.evaluate_snapshot(snapshot, "enforce", tmp_path)
    assert any("cross-family" in v or "outside the fix" in v for v in violations), violations


def test_empty_login_approval_never_counts(tmp_path):
    snapshot = {
        "body": "Closes #5",
        "changed_files": ["backend/app/services/foo.py"],
        "commits": [{"author": "insider", "files": ["backend/app/services/foo.py"]}],
        "reviews": [{"author": "", "state": "APPROVED"}],
        "issue_labels": {"5": ["high"]},
    }
    violations, _ = cce.evaluate_snapshot(snapshot, "enforce", tmp_path)
    assert any("cross-family" in v or "outside the fix" in v for v in violations), violations


def test_isolated_mode_blocks_script_dir_shadow_module(tmp_path):
    # A module planted beside the checker must not shadow stdlib json when
    # the workflow's `python -I` form runs the base copy of the checker.
    checker = tmp_path / "check_closure_evidence.py"
    checker.write_bytes((REPO_ROOT / "scripts" / "check_closure_evidence.py").read_bytes())
    (tmp_path / "json.py").write_text(
        "raise ImportError('shadow module imported')\n", encoding="utf-8"
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"body": "no close ref", "changed_files": []}), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # the checker prints em-dashes
    proc = subprocess.run(
        [sys.executable, "-I", str(checker), "evaluate", "--pr-data", str(snapshot), "--mode", "enforce"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(tmp_path),
        env=env,
    )
    assert "shadow module imported" not in (proc.stdout + proc.stderr)
    assert proc.returncode == 0
    assert "gate not applicable" in proc.stdout


def _extract_evaluate_block() -> str:
    lines = WORKFLOW_YML.read_text(encoding="utf-8").splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "run: |":
            i += 1
            base_indent = len(lines[i]) - len(lines[i].lstrip(" "))
            body: list[str] = []
            while i < len(lines):
                line = lines[i]
                if line.strip() and (len(line) - len(line.lstrip(" "))) < base_indent:
                    break
                body.append(line[base_indent:])
                i += 1
            blocks.append("\n".join(body))
        else:
            i += 1
    assert len(blocks) == 3, "expected evaluate/verify/apply-mode steps"
    return blocks[0]


def _run_preflight_case(tmp_path: Path, *, gh_succeeds: bool, git_succeeds: bool):
    """Run the REAL evaluate block under bash -e with controllable shims."""
    import shutil
    import stat

    shim = tmp_path / "shims"
    shim.mkdir(exist_ok=True)
    if gh_succeeds:
        gh_body = (
            "#!/usr/bin/env bash\n"
            'echo \'{"base": {"ref": "master"}, '
            '"head": {"sha": "cafe000000000000000000000000000000000000"}}\'\n'
            "exit 0\n"
        )
    else:
        gh_body = "#!/usr/bin/env bash\necho 'gh: api down' >&2\nexit 3\n"
    (shim / "gh").write_text(gh_body, encoding="utf-8")
    (shim / "git").write_text(
        "#!/usr/bin/env bash\n"
        + ("" if git_succeeds else "echo 'git: checkout failed' >&2\nexit 128\n")
        + "exit 0\n",
        encoding="utf-8",
    )
    # The python shim RECORDS every invocation; the checker leg must never be
    # reached when the preflight fails.
    (shim / "python").write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "' + str(tmp_path / "python.calls").replace("\\", "/") + '"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    for name in ("gh", "git", "python"):
        os.chmod(shim / name, (shim / name).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    bash = shutil.which("bash")
    if bash is None or "system32" in bash.replace("\\", "/").lower():
        bash = r"C:\Program Files\Git\bin\bash.exe"
    out_file = tmp_path / "github_output"
    env = {
        "PATH": str(shim) + os.pathsep + os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(out_file),
        "GITHUB_REPOSITORY": "O/R",
        "PR_NUMBER": "5",
        "MODE": "warn",
    }
    for key in ("SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    proc = subprocess.run(
        [bash, "-e", "-c", _extract_evaluate_block()],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    calls = ""
    calls_path = tmp_path / "python.calls"
    if calls_path.exists():
        calls = calls_path.read_text(encoding="utf-8")
    return proc, out_file.read_text(encoding="utf-8"), calls


def test_preflight_gh_failure_is_captured_and_skips_checker(tmp_path):
    proc, outputs, python_calls = _run_preflight_case(tmp_path, gh_succeeds=False, git_succeeds=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "preflight failed" in proc.stdout
    assert "eval_rc=1" in outputs
    assert "evaluate" not in python_calls, "checker must not run on a failed preflight"


def test_preflight_git_failure_is_captured_and_skips_checker(tmp_path):
    proc, outputs, python_calls = _run_preflight_case(tmp_path, gh_succeeds=True, git_succeeds=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "preflight failed" in proc.stdout
    assert "eval_rc=1" in outputs
    assert "evaluate" not in python_calls, "checker must not run on a failed preflight"


def test_preflight_success_runs_checker(tmp_path):
    proc, outputs, python_calls = _run_preflight_case(tmp_path, gh_succeeds=True, git_succeeds=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "eval_rc=0" in outputs
    assert "evaluate" in python_calls, "clean preflight must reach the checker"
