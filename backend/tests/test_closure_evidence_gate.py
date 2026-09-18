"""Proving suite for the closure-evidence gate (issue #568, E13).

Mirrors the frozen acceptance matrices (parse/evidence/cross-family/modes)
at unit level, exercises the live-fetch error policy with a monkeypatched
fetcher, and drives `verify-test` end to end against a throwaway git repo
fixture whose evidence test fails at base and passes at head.

The gate itself lives at scripts/check_closure_evidence.py (stdlib-only,
importable because scripts/ ships an __init__.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_closure_evidence as cce  # noqa: E402

BODY_WITH_EVIDENCE = (
    "Closes #55\n\n"
    "Closure evidence: backend/tests/test_x.py::test_y — re-executed via "
    "verify-test: FAILS at merge-base, PASSES at head."
)


def snapshot(labels=None, reviews=None, body=BODY_WITH_EVIDENCE, changed=None):
    changed = changed or ["backend/app/api/routes/x.py", "backend/tests/test_x.py"]
    return {
        "body": body,
        "changed_files": changed,
        "commits": [{"author": "alice", "files": changed}],
        "reviews": reviews or [{"author": "carol", "state": "APPROVED"}],
        "issue_labels": {"55": labels or []},
    }


def run_evaluate(tmp_path, snap, mode="enforce"):
    data = tmp_path / "pr.json"
    data.write_text(json.dumps(snap), encoding="utf-8")
    return cce.main(["evaluate", "--pr-data", str(data), "--mode", mode])


class TestCloseRefExtraction:
    def test_keywords_and_numbers(self):
        body = "Closes #12. Fixed #34 and resolves #56. Unrelated #78 mention, close 99."
        assert cce.extract_close_refs(body) == [12, 34, 56]

    def test_empty_and_none(self):
        assert cce.extract_close_refs("") == []
        assert cce.extract_close_refs(None) == []


class TestEvidencePrecedence:
    def test_first_test_id_wins(self):
        first, artifact, extra = cce.extract_evidence(
            "Closes #1 evidence backend/tests/test_a.py::test_one and backend/tests/test_b.py"
        )
        assert first == "backend/tests/test_a.py::test_one"
        assert artifact is None
        assert extra == ["backend/tests/test_b.py"]

    def test_artifact_extraction(self):
        first, artifact, _ = cce.extract_evidence("Closes #1. artifact: logs/run.txt")
        assert first is None
        assert artifact == "logs/run.txt"

    def test_missing_artifact_blocks_enforce(self, tmp_path, capsys):
        snap = snapshot(body="Closes #55 evidence. artifact: logs/nope.txt")
        assert run_evaluate(tmp_path, snap, "enforce") == 1
        assert "evidence" in capsys.readouterr().out.lower()

    def test_existing_artifact_passes(self, tmp_path):
        snap = snapshot(body="Closes #55 evidence. artifact: CONTRIBUTING.md")
        assert run_evaluate(tmp_path, snap, "enforce") == 0

    def test_no_evidence_blocks_with_evidence_reason(self, tmp_path, capsys):
        snap = snapshot(body="Closes #55\n\nTrust me, it is fixed.")
        assert run_evaluate(tmp_path, snap, "enforce") == 1
        assert "evidence" in capsys.readouterr().out.lower()

    def test_no_closing_reference_passes(self, tmp_path):
        snap = snapshot(body="Refactor only. No closure claimed.")
        assert run_evaluate(tmp_path, snap, "enforce") == 0


class TestCrossFamilyRule:
    @pytest.mark.parametrize("label", ["high", "critical"])
    def test_high_labels_require_cross_family(self, tmp_path, capsys, label):
        snap = snapshot(labels=[label], reviews=[{"author": "alice", "state": "APPROVED"}])
        assert run_evaluate(tmp_path, snap, "enforce") == 1
        out = capsys.readouterr().out.lower()
        assert "famil" in out and "review" in out

    @pytest.mark.parametrize("label", ["low", "medium"])
    def test_inert_labels(self, tmp_path, label):
        snap = snapshot(labels=[label], reviews=[{"author": "alice", "state": "APPROVED"}])
        assert run_evaluate(tmp_path, snap, "enforce") == 0

    def test_cross_family_approval_unblocks(self, tmp_path):
        snap = snapshot(
            labels=["high"],
            reviews=[
                {"author": "alice", "state": "APPROVED"},
                {"author": "carol", "state": "APPROVED"},
            ],
        )
        assert run_evaluate(tmp_path, snap, "enforce") == 0

    def test_any_high_ref_triggers(self, tmp_path):
        snap = snapshot(body=BODY_WITH_EVIDENCE.replace("Closes #55", "Closes #55 and Fixes #56"))
        snap["issue_labels"] = {"55": ["low"], "56": ["high"]}
        snap["reviews"] = [{"author": "alice", "state": "APPROVED"}]
        assert run_evaluate(tmp_path, snap, "enforce") == 1

    def test_bot_approval_never_counts(self, tmp_path):
        snap = snapshot(
            labels=["high"],
            reviews=[{"author": "alice", "state": "APPROVED"}, {"author": "reviewer[bot]", "state": "APPROVED"}],
        )
        assert run_evaluate(tmp_path, snap, "enforce") == 1

    def test_outsider_who_touched_only_other_families_counts(self, tmp_path):
        snap = snapshot(labels=["high"], reviews=[{"author": "bob", "state": "APPROVED"}])
        snap["commits"].append({"author": "bob", "files": ["docs/notes.md"]})
        assert run_evaluate(tmp_path, snap, "enforce") == 0


class TestModes:
    def test_warn_never_fails_but_marks(self, tmp_path, capsys):
        snap = snapshot(body="Closes #55\n\nTrust me, it is fixed.")
        assert run_evaluate(tmp_path, snap, "warn") == 0
        assert "closure-evidence: WARN" in capsys.readouterr().out

    def test_enforce_fails(self, tmp_path):
        snap = snapshot(body="Closes #55\n\nTrust me, it is fixed.")
        assert run_evaluate(tmp_path, snap, "enforce") == 1

    def test_evidence_test_line_emitted_once(self, tmp_path, capsys):
        snap = snapshot(
            body="Closes #55 evidence backend/tests/test_a.py::test_one and backend/tests/test_b.py::two"
        )
        assert run_evaluate(tmp_path, snap, "enforce") == 0
        lines = [
            ln
            for ln in capsys.readouterr().out.splitlines()
            if ln.startswith("closure-evidence: evidence-test")
        ]
        assert lines == ["closure-evidence: evidence-test backend/tests/test_a.py::test_one"]


class TestFamilyMapping:
    @pytest.mark.parametrize(
        "path,family",
        [
            ("backend/app/api/routes/chat.py", "backend-api"),
            ("backend/app/services/rag_engine.py", "backend-services"),
            ("backend/app/models/memory.py", "backend-models"),
            ("backend/app/core.py", "backend-core"),
            ("backend/tests/test_x.py", "backend-tests"),
            ("frontend/src/lib/api.ts", "frontend-src"),
            ("scripts/check_closure_evidence.py", "tooling-scripts"),
            (".github/workflows/ci.yml", "ci-workflows"),
            ("docs/ci/closure-evidence-gate.md", "docs"),
            ("README.md", "README.md"),
            ("other/deep/file.txt", "other/deep"),
        ],
    )
    def test_prefixes(self, path, family):
        assert cce.family_of(path) == family


class TestLiveFetchPolicy:
    def test_fetch_error_mode_governed_exit(self, tmp_path, capsys, monkeypatch):
        def boom(endpoint):
            raise cce.FetchError("gh CLI not available: nope")

        monkeypatch.setattr(cce, "gh_api", boom)
        assert cce.main(["evaluate", "--pr", "55", "--repo-slug", "ZaxbyHub/ragappv3", "--mode", "warn"]) == 0
        assert "closure-evidence: ERROR" in capsys.readouterr().out
        assert cce.main(["evaluate", "--pr", "55", "--repo-slug", "ZaxbyHub/ragappv3", "--mode", "enforce"]) == 1

    def test_gh_api_retries_once_then_raises(self, monkeypatch):
        results = []

        class Fail:
            returncode = 1
            stdout = ""
            stderr = "Bad gateway"

        def fake_gh(args):
            results.append(args)
            return Fail()

        monkeypatch.setattr(cce, "_gh", fake_gh)
        monkeypatch.setattr(cce.time, "sleep", lambda s: None)
        with pytest.raises(cce.FetchError):
            cce.gh_api("repos/x/y")
        assert len(results) == 2  # one retry

    def test_issue_404_is_skipped_not_fatal(self, monkeypatch):
        payloads = {
            "repos/O/R/pulls/7": {"body": "Closes #5 evidence backend/tests/test_a.py::x"},
            "repos/O/R/pulls/7/files?per_page=100": [{"filename": "backend/app/api/x.py"}],
            "repos/O/R/pulls/7/commits?per_page=100": [{"author": {"login": "alice"}, "commit": {"author": {}}}],
            "repos/O/R/pulls/7/reviews?per_page=100": [{"user": {"login": "alice"}, "state": "APPROVED"}],
            "repos/O/R/issues/5": None,  # marker: raises LookupError
        }

        def fake_gh_api(endpoint):
            value = payloads[endpoint]
            if value is None:
                raise LookupError(endpoint)
            if endpoint.endswith("commits?per_page=100"):
                return value
            if endpoint.startswith("repos/O/R/pulls/7/commits/http"):
                return {"files": [{"filename": "backend/app/api/x.py"}]}
            return value

        # commits list entries carry a `url` the gate fetches per-commit
        payloads["repos/O/R/pulls/7/commits?per_page=100"] = [
            {"author": {"login": "alice"}, "commit": {"author": {}}, "url": "repos/O/R/commits/abc"}
        ]
        monkeypatch.setattr(cce, "gh_api", fake_gh_api)
        snap = cce.fetch_snapshot(7, "O/R")
        assert snap["issue_labels"] == {}  # 404 ref skipped
        assert snap["body"].startswith("Closes #5")


FIXTURE_TEST = '''import unittest
from pathlib import Path


class TestDummyGate(unittest.TestCase):
    def test_marker_content(self):
        marker = Path(__file__).resolve().parent / "marker.txt"
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "fixed")
'''


def build_fixture_repo(base: Path) -> dict:
    def g(*args):
        proc = subprocess.run(
            ["git", "-C", str(base), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    g("init")
    g("config", "user.email", "gate-test@example.invalid")
    g("config", "user.name", "Gate Test")
    g("config", "commit.gpgsign", "false")
    tests = base / "backend" / "tests"
    tests.mkdir(parents=True)
    (base / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (base / "backend" / "__init__.py").write_text("", encoding="utf-8")
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_dummy_gate.py").write_text(FIXTURE_TEST, encoding="utf-8")
    shas = {}
    for label, marker in (("broken", "broken"), ("fixed", "fixed"), ("broken2", "broken")):
        (tests / "marker.txt").write_text(marker + "\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-m", f"fixture {label}")
        shas[label] = g("rev-parse", "HEAD")
    return shas


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("verify_fixture") / "repo"
    repo.mkdir()
    return repo, build_fixture_repo(repo)


class TestVerifyTest:
    def _verify(self, repo, base, head, test="backend/tests/test_dummy_gate.py"):
        return cce.main(["verify-test", "--repo", str(repo), "--base", base, "--head", head, "--test", test])

    def test_fail_at_base_pass_at_head_ok(self, fixture_repo, capsys):
        repo, shas = fixture_repo
        assert self._verify(repo, shas["broken"], shas["fixed"]) == 0
        assert "fails at base" in capsys.readouterr().out

    def test_pass_at_base_is_343_pattern(self, fixture_repo, capsys):
        repo, shas = fixture_repo
        assert self._verify(repo, shas["fixed"], shas["broken2"]) == 1
        out = capsys.readouterr().out.lower()
        assert "base" in out and "#343" in out

    def test_fail_at_head_rejected(self, fixture_repo):
        repo, shas = fixture_repo
        assert self._verify(repo, shas["broken"], shas["broken2"]) == 1

    def test_base_equals_head_passing_is_343_pattern(self, fixture_repo):
        repo, shas = fixture_repo
        assert self._verify(repo, shas["fixed"], shas["fixed"]) == 1

    def test_node_id_without_pytest_fails_closed(self, fixture_repo, monkeypatch, capsys):
        repo, shas = fixture_repo
        monkeypatch.setattr(cce, "_pytest_available", lambda: False)
        rc = self._verify(
            repo, shas["broken"], shas["fixed"], test="backend/tests/test_dummy_gate.py::TestDummyGate::test_marker_content"
        )
        assert rc == 1
        assert "fail-closed" in capsys.readouterr().out

    def test_unittest_fallback_when_pytest_absent(self, fixture_repo, monkeypatch):
        repo, shas = fixture_repo
        monkeypatch.setattr(cce, "_pytest_available", lambda: False)
        assert self._verify(repo, shas["broken"], shas["fixed"]) == 0
