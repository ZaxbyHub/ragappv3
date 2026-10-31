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


def _extract_run_blocks() -> list[str]:
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
    return blocks


def _extract_evaluate_block() -> str:
    return _extract_run_blocks()[0]


def _run_preflight_case(tmp_path: Path, *, gh_succeeds: bool, git_succeeds: bool, dispatch: bool = False):
    """Run the REAL evaluate block under bash -e with controllable shims.

    The shims are payload-faithful: the python shim derives base.ref /
    head.sha FROM the stdin payload (an empty or broken pipe/derivation
    therefore yields an empty ref that the git-args assertions reject), and
    the git shim records its argument lines so the tests assert WHICH ref
    and pathspec the restore touched (#1049 review PRR-008).
    """
    import shutil
    import stat

    calls_file = str(tmp_path / "python.calls").replace("\\", "/")
    git_calls_file = str(tmp_path / "git.calls").replace("\\", "/")
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
    git_fail = "" if git_succeeds else "echo 'git: failed' >&2\nexit 128\n"
    (shim / "git").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{git_calls_file}"\n'
        + git_fail
        + "exit 0\n",
        encoding="utf-8",
    )
    # The python shim records every invocation AND faithfully parses the two
    # workflow JSON fields from stdin (keyed on the -c program's argv, value
    # extracted from the actual payload).
    (shim / "python").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{calls_file}"\n'
        "case \"$*\" in\n"
        "  *evaluate*) exit 0 ;;\n"
        "  *\"'base'\"*) payload=\"$(cat)\"; printf '%s' \"$payload\" | sed -n 's/.*\"base\": *{\"ref\": *\"\\([^\"]*\\)\".*/\\1/p'; exit 0 ;;\n"
        "  *\"'head'\"*) payload=\"$(cat)\"; printf '%s' \"$payload\" | sed -n 's/.*\"head\": *{\"sha\": *\"\\([^\"]*\\)\".*/\\1/p'; exit 0 ;;\n"
        "esac\n"
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
    if dispatch:
        env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
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
    git_calls = ""
    git_calls_path = tmp_path / "git.calls"
    if git_calls_path.exists():
        git_calls = git_calls_path.read_text(encoding="utf-8")
    return proc, out_file.read_text(encoding="utf-8"), calls, git_calls


def test_preflight_gh_failure_is_captured_and_skips_checker(tmp_path):
    proc, outputs, python_calls, _git_calls = _run_preflight_case(tmp_path, gh_succeeds=False, git_succeeds=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "preflight failed" in proc.stdout
    assert "eval_rc=1" in outputs
    assert "gate_ran=0" in outputs, "a failed preflight must record gate_ran=0"
    assert "evaluate" not in python_calls, "checker must not run on a failed preflight"


def test_preflight_git_failure_is_captured_and_skips_checker(tmp_path):
    proc, outputs, python_calls, _git_calls = _run_preflight_case(tmp_path, gh_succeeds=True, git_succeeds=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "preflight failed" in proc.stdout
    assert "eval_rc=1" in outputs
    assert "gate_ran=0" in outputs, "a failed preflight must record gate_ran=0"
    assert "evaluate" not in python_calls, "checker must not run on a failed preflight"


def test_preflight_success_runs_checker(tmp_path):
    proc, outputs, python_calls, git_calls = _run_preflight_case(tmp_path, gh_succeeds=True, git_succeeds=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "eval_rc=0" in outputs
    assert "gate_ran=1" in outputs
    assert "evaluate" in python_calls, "clean preflight must reach the checker"
    # PRR-008: the restore must target the BASE ref derived from the payload
    # (gh shim serves base.ref=master) — a HEAD-ref or empty-ref restore
    # (the exact self-grading #741 forbids) now fails these assertions.
    assert "checkout origin/master -- scripts/" in git_calls, git_calls
    assert "checkout HEAD -- scripts/" not in git_calls, git_calls


def test_preflight_dispatch_fetches_and_checks_out_pr_head(tmp_path):
    # PRR-006/PRR-008: the dispatch branch must fetch the PR head ref (fork
    # heads are not in the base repo's object database) and check out the
    # payload's head SHA before restoring the base scripts tree.
    proc, outputs, python_calls, git_calls = _run_preflight_case(
        tmp_path, gh_succeeds=True, git_succeeds=True, dispatch=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "eval_rc=0" in outputs
    assert "gate_ran=1" in outputs
    assert "fetch origin refs/pull/5/head" in git_calls, git_calls
    assert "checkout cafe000000000000000000000000000000000000" in git_calls, git_calls
    assert "checkout origin/master -- scripts/" in git_calls, git_calls
    assert "evaluate" in python_calls


# ── #742 final-critic round 1: the positive "exactly one CI venue" leg ────────

FRONTEND_TOOLCHAIN_NODES = (
    "test_issue258_coverage_gate.py::test_ac11_coverage_command_passes",
    "test_issue258_build_contracts.py::test_ac8_vite_node_config_project_typechecks",
)


def test_frontend_toolchain_nodes_have_exactly_one_venue():
    # The negative leg (no in-repo frontend install in the Backend job) is
    # the frozen C12 check; this pins the POSITIVE leg — each node is named
    # by exactly one job's steps, that job is `frontend` (where the tree the
    # nodes need actually exists), and the venue step fails on a skip.
    import yaml as _yaml

    ci = _yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    venues: dict[str, list[str]] = {}
    for job_id, job in ci["jobs"].items():
        steps_text = "\n".join(str(s.get("run") or "") for s in job.get("steps") or [])
        for node in FRONTEND_TOOLCHAIN_NODES:
            if node in steps_text:
                venues.setdefault(node, []).append(job_id)
    assert venues == {node: ["frontend"] for node in FRONTEND_TOOLCHAIN_NODES}, venues
    venue_steps = [
        s for s in ci["jobs"]["frontend"]["steps"]
        if any(node in str(s.get("run") or "") for node in FRONTEND_TOOLCHAIN_NODES)
    ]
    assert len(venue_steps) == 1
    venue_run = str(venue_steps[0].get("run") or "")
    assert "skipped" in venue_run and "exit 1" in venue_run.replace("::error::", ""), (
        "the venue step must fail the job when a node skips in its only venue"
    )
    # Round-2 review findings: the venue modules import yaml at collection
    # time AND the shared backend conftest's autouse fixtures import the app
    # stack, so the venue needs the SAME provision the Backend job's pytest
    # runs under (runtime lock + dev requirements) — exactly the environment
    # these nodes ran green in before #742. A pytest-only provision fails at
    # collection (ModuleNotFoundError) on every run.
    assert "requirements-lock-ci.txt" in venue_run and "requirements-dev.txt" in venue_run, (
        "the venue step must install the Backend job's pytest provision "
        "(runtime lock + dev requirements) or it cannot collect its modules"
    )


# ── #1049 feedback round: venue-order, nightly-scan, positive-control pins ───

def test_venue_step_precedes_coverage_gate():
    # PRR-016: the venue step is load-bearing ORDERED — test_ac11 asserts a
    # clean frontend/coverage slate, and the job's later "Coverage gate" step
    # creates that directory. Nothing else pins the ordering.
    import yaml as _yaml

    ci = _yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = ci["jobs"]["frontend"]["steps"]
    venue_idx = next(
        i for i, s in enumerate(steps)
        if "test_issue258_coverage_gate.py::test_ac11_coverage_command_passes" in str(s.get("run") or "")
    )
    coverage_idx = next(
        i for i, s in enumerate(steps)
        if "test:coverage" in str(s.get("run") or "")
    )
    assert venue_idx < coverage_idx, (venue_idx, coverage_idx)


def test_no_second_venue_in_nightly_workflows():
    # PRR-020: the exactly-one-venue pin scans ci.yml only; these scans keep
    # the nightly workflows visible. A second venue needs BOTH a frontend
    # tree (npm ci under working-directory frontend) AND a backend pytest run
    # over tests/ in the same workflow — the stryker job installs a frontend
    # tree but runs no backend pytest, and nightly runs pytest but installs
    # no frontend tree. Assert the node ids never appear and the two halves
    # never combine.
    import re as _re

    nightly = (REPO_ROOT / ".github" / "workflows" / "nightly.yml")
    gates = (REPO_ROOT / ".github" / "workflows" / "nightly-quality-gates.yml")
    for path in (nightly, gates):
        assert path.exists(), path
        text = path.read_text(encoding="utf-8")
        for node in FRONTEND_TOOLCHAIN_NODES:
            assert node not in text, f"{path.name} must not name {node}"
        installs_frontend = "npm ci" in text and "working-directory: frontend" in text
        runs_backend_pytest = _re.search(r"run:[^\n]*pytest[^\n]*tests/", text) is not None
        assert not (installs_frontend and runs_backend_pytest), (
            f"{path.name} combines a frontend install with a backend pytest "
            "run — it would silently become a second venue for the "
            "frontend-toolchain nodes"
        )


def test_engines_positive_control_and_non_admitted_majors():
    # PRR-011/PRR-014: the frozen C14 test proves only that 22.22.0/.1 are
    # EXCLUDED; this positive control proves the shipped engines range still
    # ADMITS the supported versions (a nonsensical ">=99" range would pass
    # C14) and rejects the majors the lockfile's dependency engines reject.
    # Read the range from the package.json itself (no hardcoded duplicate).
    import json as _json

    from test_h04_ci_hygiene import _satisfies_range, _version_tuple

    engines_node = _json.loads(
        (REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )["engines"]["node"]
    for admitted in ("22.22.2", "22.22.3", "24.15.0", "26.0.0"):
        assert _satisfies_range(_version_tuple(admitted), engines_node), (admitted, engines_node)
    for rejected in ("22.22.0", "22.22.1", "23.0.0", "25.1.0"):
        assert not _satisfies_range(_version_tuple(rejected), engines_node), (rejected, engines_node)


# ── #1049 review round 2: the gate_ran seam must be wired end to end ─────────

def test_gate_ran_output_is_wired_into_apply_env():
    # Round-2 reviewer Critical: Evaluate writes `gate_ran` but Apply never
    # read it (no env mapping), so the ERROR-gate-not-run branch was dead in
    # every real run and preflight failures kept the violation labels. Pin
    # the seam: the output key exists AND the Apply env maps it.
    text = WORKFLOW_YML.read_text(encoding="utf-8")
    assert 'echo "gate_ran=$GATE_RAN"' in text, "evaluate must emit gate_ran"
    assert "GATE_RAN: ${{ steps.evaluate.outputs.gate_ran }}" in text, (
        "the Apply step env must map steps.evaluate.outputs.gate_ran — "
        "without the mapping GATE_RAN is unset in every real run and the "
        "ERROR-gate-not-run branch is dead"
    )


def _run_apply_block(tmp_path: Path, *, gate_ran: str, mode: str):
    """Execute the REAL Apply run block under bash -e with given outputs."""
    import shutil

    apply_block = _extract_run_blocks()[2]
    bash = shutil.which("bash")
    if bash is None or "system32" in bash.replace("\\", "/").lower():
        bash = r"C:\Program Files\Git\bin\bash.exe"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "EVAL_RC": "1",
        "VERIFY_RC": "",
        "MODE": mode,
        "GATE_RAN": gate_ran,
    }
    for key in ("SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    return subprocess.run(
        [bash, "-e", "-c", apply_block],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_apply_gate_not_run_warn_is_nonblocking_and_enforce_fails(tmp_path):
    # PRR-007 closure, executed against the real Apply block: a gate that
    # never ran must NOT emit the violation tokens (FAIL/WARN), in either
    # mode.
    warn = _run_apply_block(tmp_path, gate_ran="0", mode="warn")
    assert warn.returncode == 0, warn.stdout + warn.stderr
    assert "gate not run" in warn.stdout
    assert "no violation observed" in warn.stdout
    assert "WARN — violation observed" not in warn.stdout
    assert "FAIL — enforce-mode violation" not in warn.stdout
    enforce = _run_apply_block(tmp_path, gate_ran="0", mode="enforce")
    assert enforce.returncode == 1
    assert "gate not run" in enforce.stdout
    assert "FAIL — enforce-mode violation" not in enforce.stdout


def test_apply_legacy_unset_gate_ran_keeps_violation_semantics(tmp_path):
    # GATE_RAN unset (legacy outputs, and the frozen gate-suite shell
    # contracts) keeps the historical violation labels — the frozen
    # test_closure_evidence_gate.py apply tests pin exactly this shape.
    warn = _run_apply_block(tmp_path, gate_ran="", mode="warn")
    assert warn.returncode == 0
    assert "WARN — violation observed" in warn.stdout
