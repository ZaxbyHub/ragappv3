"""Acceptance checks for issue #741 (workstream H, PR 3 of 10): the
closure-evidence gate stops misclassifying evidence.

Scope — the three mechanical defects of scripts/check_closure_evidence.py
this PR delivers, one node each:

* URL-form artifacts are advisory-only (docs/ci/closure-evidence-gate.md):
  a body whose only evidence is ``artifact: https://...`` must NOT yield a
  false "missing from the repository" violation (D1).
* The markdown-noise normalization applied for close-ref extraction must
  also reach the artifact capture: ``artifact: `docs/evidence/capture.md``
  must resolve to the real path, not keep its backticks and fail is_file()
  (D2).
* ``fetch_snapshot`` must never silently truncate at GitHub's 100-item page
  boundary: a PR with 150 commits / 150 changed files must come back
  complete (paginated) or raise FetchError (fail-closed) — never a quietly
  shortened list (D3).
* D4 (now decided): only each reviewer's LATEST non-COMMENTED review counts
  toward the cross-family approval requirement of high/critical issues — a
  historical APPROVED superseded by the same login's CHANGES_REQUESTED must
  not satisfy it.
* The gate workflow (.github/workflows/closure-evidence.yml) must restore
  the BASE branch's copy of scripts/ before invoking the checker, so a PR
  that edits the checker cannot grade itself.

Verdict contract (issue-tracer): every node is RED at base, GREEN after the
fix (the D4 node's second leg also passes at base — its first leg is the
discriminator). The pagination nodes run ``fetch_snapshot`` against a
monkeypatched ``cce._gh`` that serves the whole endpoint set from memory
(tolerant of both single-request and paginating implementations) — no
network is touched. The base-copy pin is a structural text pin in the style
of test_1021_timeout_wiring.py: it tolerates any base-ref expression before
the ``-- scripts`` pathspec (and ``-- scripts/``).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_closure_evidence as cce  # noqa: E402

REPO_SLUG = "ZaxbyHub/ragappv3"
PR_NUMBER = 1
GATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "closure-evidence.yml"


def _snapshot(body: str) -> dict:
    """Minimal offline snapshot: one low-label close ref, no reviews/commits."""
    return {
        "body": body,
        "changed_files": ["backend/app/services/foo.py"],
        "commits": [],
        "reviews": [],
        "issue_labels": {"5": []},
    }


def test_url_artifact_is_advisory_not_missing(tmp_path):
    # D1 (RED at base, GREEN after the fix): resolving a URL-form artifact
    # as a repository path manufactures a false "missing from the repository"
    # violation; URL-form evidence is advisory-only.
    body = (
        "Closes #5\n\n"
        "Evidence: artifact: https://github.com/ZaxbyHub/ragappv3/actions/runs/1"
    )
    violations, _ = cce.evaluate_snapshot(_snapshot(body), "enforce", tmp_path)
    assert len(violations) == 0


def test_backtick_wrapped_artifact_resolves(tmp_path):
    # D2 (RED at base, GREEN after the fix): backticks around an existing
    # artifact path must be stripped before resolution; keeping them makes
    # is_file() fail even though the file exists.
    capture = tmp_path / "docs" / "evidence" / "capture.md"
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text("# capture\n", encoding="utf-8")
    body = "Closes #5\n\nEvidence: artifact: `docs/evidence/capture.md`\n"
    violations, _ = cce.evaluate_snapshot(_snapshot(body), "enforce", tmp_path)
    assert len(violations) == 0


def _commit_entry(i: int) -> dict:
    return {
        "author": {"login": f"committer{i}"},
        "commit": {"author": {"name": f"Committer {i}"}},
        "url": f"https://api.github.com/repos/{REPO_SLUG}/commits/c{i:04d}",
    }


def _install_gh_stub(monkeypatch, *, commits: list[dict], files: list[dict]) -> None:
    """Serve every endpoint fetch_snapshot reaches for from memory.

    Pagination-tolerant by design: any ``per_page=N`` / ``page=M`` query is
    honored (page 1 when no page param is present), so both the current
    single-request implementation and a future paginating one are served
    without network. Endpoints are matched by substring in the order
    fetch_snapshot asks for them.
    """

    def _page_slice(endpoint: str, full: list) -> list:
        per_match = re.search(r"per_page=(\d+)", endpoint)
        per_page = int(per_match.group(1)) if per_match else 100
        page_match = re.search(r"[?&]page=(\d+)", endpoint)
        page = int(page_match.group(1)) if page_match else 1
        return full[(page - 1) * per_page : page * per_page]

    def fake_gh(args: list[str]) -> SimpleNamespace:
        endpoint = args[0]
        if endpoint.startswith("http"):
            # Per-commit detail fetch: entry["url"] is a full api.github.com URL.
            payload: object = {"files": [{"filename": "backend/app/services/x.py"}]}
        elif "/commits?" in endpoint:
            payload = _page_slice(endpoint, commits)
        elif "/files?" in endpoint:
            payload = _page_slice(endpoint, files)
        elif "/reviews?" in endpoint:
            payload = []
        elif "/issues/" in endpoint:
            payload = {"labels": []}
        elif "/pulls/" in endpoint:
            payload = {"body": "Closes #5"}
        else:  # pragma: no cover - unknown endpoint must fail loudly, not lie
            return SimpleNamespace(returncode=0, stdout="not-json", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(cce, "_gh", fake_gh)


def test_commit_list_beyond_100_is_not_silently_truncated(monkeypatch):
    # D3 (RED at base, GREEN after the fix): a PR with 150 commits must be
    # fetched complete (paginated) or fail closed — a single unpaginated
    # request that silently drops commits 101-150 is the defect.
    commits = [_commit_entry(i) for i in range(150)]
    files = [{"filename": f"backend/app/services/f{i}.py"} for i in range(3)]
    _install_gh_stub(monkeypatch, commits=commits, files=files)
    try:
        snapshot = cce.fetch_snapshot(PR_NUMBER, REPO_SLUG)
    except cce.FetchError:
        outcome = "failed-closed"
    else:
        outcome = "complete" if len(snapshot["commits"]) == 150 else "truncated"
    assert outcome in ("complete", "failed-closed")


def test_file_list_beyond_100_is_not_silently_truncated(monkeypatch):
    # D3 (RED at base, GREEN after the fix): a PR with 150 changed files must
    # be fetched complete (paginated) or fail closed — a single unpaginated
    # request that silently drops files 101-150 is the defect.
    commits = [_commit_entry(i) for i in range(2)]
    files = [{"filename": f"backend/app/services/file_{i:03d}.py"} for i in range(150)]
    _install_gh_stub(monkeypatch, commits=commits, files=files)
    try:
        snapshot = cce.fetch_snapshot(PR_NUMBER, REPO_SLUG)
    except cce.FetchError:
        outcome = "failed-closed"
    else:
        outcome = "complete" if len(snapshot["changed_files"]) == 150 else "truncated"
    assert outcome in ("complete", "failed-closed")


def test_superseded_approval_does_not_satisfy_cross_family(tmp_path):
    # D4, decided rule (RED at base, GREEN after the fix): only each
    # reviewer's LATEST non-COMMENTED review counts toward the cross-family
    # approval requirement. The body carries no evidence of any kind, so the
    # missing-evidence violation is always present; the question is only
    # whether the high-label cross-family violation joins it.
    leg_a = {
        "body": "Closes #5",
        "changed_files": ["backend/app/services/foo.py"],
        "commits": [{"author": "insider", "files": ["backend/app/services/foo.py"]}],
        "reviews": [
            {"author": "insider", "state": "APPROVED"},
            {"author": "approver1", "state": "APPROVED"},
            {"author": "approver1", "state": "CHANGES_REQUESTED"},
        ],
        "issue_labels": {"5": ["high"]},
    }
    # Leg A: approver1's APPROVED is superseded by their own CHANGES_REQUESTED,
    # so it must NOT count — after the fix both violations appear (missing
    # evidence + cross-family). At base the historical APPROVED still
    # satisfies cross-family, leaving only the missing-evidence violation.
    violations_a, _ = cce.evaluate_snapshot(leg_a, "enforce", tmp_path)
    assert len(violations_a) == 2

    # Leg B: approver2's latest non-COMMENTED review IS an approval, so the
    # cross-family requirement is satisfied and only the missing-evidence
    # violation remains (passes before and after the fix).
    leg_b = dict(leg_a)
    leg_b["reviews"] = [
        {"author": "insider", "state": "APPROVED"},
        {"author": "approver2", "state": "CHANGES_REQUESTED"},
        {"author": "approver2", "state": "APPROVED"},
    ]
    violations_b, _ = cce.evaluate_snapshot(leg_b, "enforce", tmp_path)
    assert len(violations_b) == 1


def test_gate_runs_base_copy_of_checker():
    # Decided AC6 remedy (RED at base, GREEN after the fix): the gate
    # workflow must restore the BASE branch's copy of scripts/ before
    # invoking the checker — otherwise a PR that edits
    # scripts/check_closure_evidence.py grades itself with its own edits.
    # Structural text pin: any base-ref expression is tolerated before the
    # ``-- scripts`` pathspec (``-- scripts/`` accepted too).
    yml = GATE_WORKFLOW.read_text(encoding="utf-8")
    has_checkout = "git checkout" in yml
    has_scripts_pathspec = re.search(r"--\s*scripts/?\s*$", yml, re.M) is not None
    restored = has_checkout and has_scripts_pathspec
    assert restored is True
