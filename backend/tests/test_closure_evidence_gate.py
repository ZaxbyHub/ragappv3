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
import os
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

    @pytest.mark.parametrize(
        "body",
        [
            "Closes:#1",          # colon separator, no space
            "closes: #1",         # colon separator with space (lowercase)
            "**Closes** #1",      # markdown bold around the keyword
            "`Fixes` #2",         # backtick-wrapped keyword
            "Resolves\u00a0#3",   # non-breaking space separator
            "fixed#4",            # no separator at all
        ],
    )
    def test_github_lenient_forms_detected(self, body):
        # GitHub resolves closing keywords after markdown rendering, so all of
        # these auto-close issues; the gate must detect every one of them.
        assert cce.extract_close_refs(body), f"closing ref not detected in: {body!r}"

    def test_underscore_is_not_a_separator(self):
        # A word character between keyword and # means the token is not the
        # bare keyword (closes_ is not "closes"); GitHub does not close these.
        assert cce.extract_close_refs("closes_#1") == []


class TestReviewRound1Fixes:
    """Regression tests for the independent implementation-review findings."""

    def test_artifact_path_traversal_rejected(self, tmp_path, capsys):
        snap = snapshot(body="Closes #55 evidence. artifact: ../outside-repo.md")
        assert run_evaluate(tmp_path, snap, "enforce") == 1
        assert "evidence" in capsys.readouterr().out.lower()

    def test_empty_files_commit_is_conservative_same_family(self, tmp_path):
        # A commit with an unfetched file list (live-mode >100-commit
        # truncation) must not qualify its author as cross-family.
        snap = snapshot(labels=["high"], reviews=[{"author": "bob", "state": "APPROVED"}])
        snap["commits"] = [
            {"author": "alice", "files": ["backend/app/api/routes/x.py"]},
            {"author": "bob", "files": []},
        ]
        assert run_evaluate(tmp_path, snap, "enforce") == 1

    def test_unicode_node_id_not_truncated(self):
        first, _, extra = cce.extract_evidence(
            "Closes #1 evidence backend/tests/test_foo.py::test_функция end"
        )
        assert first == "backend/tests/test_foo.py::test_функция"

    def test_extract_evidence_no_test_id_returns_empty_additional(self):
        # Copilot review (id 4052018499): when the body names no pytest id,
        # extract_evidence must return additional_test_ids as [] (a flat
        # list), not [[]] (a list containing the empty list). The earlier
        # `first, *extra = (None, [])` bug set extra = [[]]; any future
        # consumer of the third value would have seen a nested empty list.
        first, artifact, extra = cce.extract_evidence(
            "Closes #1\n\nClosure evidence. artifact: CONTRIBUTING.md"
        )
        assert first is None
        assert artifact == "CONTRIBUTING.md"
        assert extra == []
        assert isinstance(extra, list)
        assert all(isinstance(item, str) for item in extra)


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


WORKFLOW_REL = REPO_ROOT / ".github" / "workflows" / "closure-evidence.yml"


def extract_run_blocks(workflow_text: str) -> list[str]:
    """Return the `run: |` block bodies of the workflow, in file order."""
    blocks: list[str] = []
    lines = workflow_text.splitlines()
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
    return blocks


def extract_top_keys(workflow_text: str) -> list[tuple[str, list[str]]]:
    """Return the list of top-level YAML keys with their nested lines.

    Stdlib only — indentation-aware, same parser family as parse_on_block in
    check_ac1_wiring_parse.py. Used to assert presence/absence of `on:` events
    without depending on PyYAML.
    """
    keys: list[tuple[str, list[str]]] = []
    lines = workflow_text.splitlines()
    top_indent: int | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if top_indent is None:
            top_indent = indent
            if not stripped.endswith(":"):
                i += 1
                continue
            key = stripped[:-1].strip().strip("\"'")
            keys.append((key, []))
            i += 1
            continue
        if indent < top_indent:
            break
        if indent == top_indent and stripped.endswith(":"):
            key = stripped[:-1].strip().strip("\"'")
            keys.append((key, []))
            i += 1
            continue
        if keys:
            keys[-1][1].append(line)
        i += 1
    return keys


def extract_subkeys(key_lines: list[str]) -> list[tuple[str, list[str]]]:
    sub: list[tuple[str, list[str]]] = []
    if not key_lines:
        return sub
    base_indent = None
    for raw in key_lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if base_indent is None:
            base_indent = indent
        if indent < base_indent:
            break
        if indent == base_indent and raw.strip().endswith(":"):
            subkey = raw.strip()[:-1].strip().strip("\"'")
            sub.append((subkey, []))
        elif sub:
            sub[-1][1].append(raw)
    return sub


class TestWorkflowShellContract:
    """The workflow's own step scripts must survive `bash -e` on failing runs.

    GitHub Actions executes `run: |` blocks with `bash -e`: a failing
    verification (the normal violating-PR case) must still reach the
    mode-application step. These tests extract the REAL step scripts from the
    workflow file and run them under `bash -e` with shimmed binaries.
    """

    def setup_method(self):
        run_blocks = extract_run_blocks(WORKFLOW_REL.read_text(encoding="utf-8"))
        assert len(run_blocks) == 3, "expected evaluate/verify/apply-mode steps"
        self.evaluate_step, self.verify_step, self.apply_step = run_blocks

    def _shim_dir(self, tmp_path, python_behaviour: str):
        shim = tmp_path / "shims"
        shim.mkdir(exist_ok=True)
        (shim / "gh").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"api"* ]]; then echo \'{"base": {"ref": "master"}, '
            '"head": {"sha": "cafe000000000000000000000000000000000000"}}\'; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (shim / "git").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"merge-base"* ]]; then echo '
            '"beef000000000000000000000000000000000000"; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        if python_behaviour == "failing_verify":
            body = (
                "#!/usr/bin/env bash\n"
                'if [[ "$*" == *"verify-test"* ]]; then\n'
                '  echo "closure-evidence: FAIL - simulated failing verification"\n'
                "  exit 1\n"
                "fi\n"
                'if [[ "$*" == *"pip"* ]]; then exit 0; fi\n'
                'if [[ "$*" == *"base\\x5d\\x5b\\x27ref\\x27"* ]]; then echo master; exit 0; fi\n'
                'if [[ "$*" == *"head"* ]]; then echo '
                "cafe000000000000000000000000000000000000; exit 0; fi\n"
                "exit 0\n"
            )
        else:  # failing_evaluate
            body = (
                "#!/usr/bin/env bash\n"
                'if [[ "$*" == *"evaluate"* ]]; then\n'
                '  echo "closure-evidence: ERROR - simulated failing evaluate"\n'
                "  exit 1\n"
                "fi\n"
                "exit 0\n"
            )
        (shim / "python").write_text(body, encoding="utf-8")
        for name in ("gh", "git", "python"):
            import stat

            os.chmod(shim / name, os.stat(shim / name).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return shim

    def _real_bash(self):
        """Locate a working bash: shutil.which may return the Windows WSL
        launcher stub (System32\\bash.exe), which is not a shell here."""
        import shutil

        candidate = shutil.which("bash")
        candidates = [candidate] if candidate else []
        candidates += [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            "/usr/bin/bash",
            "/bin/bash",
        ]
        for path in candidates:
            if not path:
                continue
            if "system32" in path.replace("\\", "/").lower():
                continue  # WSL launcher stub
            try:
                probe = subprocess.run(
                    [path, "-c", "echo ok"],
                    capture_output=True, text=True, timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0 and probe.stdout.strip() == "ok":
                return path
        return None

    def _run_step(self, script: str, tmp_path: Path, env_extra: dict):
        bash = self._real_bash()
        if bash is None:  # pragma: no cover - CI and Git Bash both provide bash
            pytest.skip("no working bash available")
        bash_dir = str(Path(bash).resolve().parent)
        out_file = tmp_path / "github_output"
        # Shims first (they shadow python/gh/git), then the real bash's dir so
        # the shims' `#!/usr/bin/env bash` shebangs resolve, then the ambient
        # PATH; keep core Windows vars so native binaries do not misbehave.
        env = {"PATH": str(tmp_path / "shims") + os.pathsep + bash_dir + os.pathsep + os.environ.get("PATH", "")}
        for key in ("SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
        env["GITHUB_OUTPUT"] = str(out_file)
        env.update(env_extra)
        proc = subprocess.run(
            [bash, "-e", "-c", script],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        return proc, out_file

    def test_failing_evaluate_is_captured_not_fatal(self, tmp_path):
        self._shim_dir(tmp_path, "failing_evaluate")
        proc, out_file = self._run_step(
            self.evaluate_step,
            tmp_path,
            {"PR_NUMBER": "5", "MODE": "warn", "GITHUB_REPOSITORY": "O/R"},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "simulated failing evaluate" in proc.stdout  # log still catted
        assert "eval_rc=1" in out_file.read_text(encoding="utf-8")

    def test_failing_verify_is_captured_not_fatal_in_warn(self, tmp_path):
        self._shim_dir(tmp_path, "failing_verify")
        proc, out_file = self._run_step(
            self.verify_step,
            tmp_path,
            {"PR_NUMBER": "5", "TEST_ID": "backend/tests/test_x.py::test_y", "GITHUB_REPOSITORY": "O/R"},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "simulated failing verification" in proc.stdout
        assert "verify_rc=1" in out_file.read_text(encoding="utf-8")

    def test_apply_mode_warn_exits_zero_with_marker(self, tmp_path):
        proc, _ = self._run_step(
            self.apply_step, tmp_path, {"EVAL_RC": "1", "VERIFY_RC": "1", "MODE": "warn"}
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "closure-evidence: WARN" in proc.stdout

    def test_apply_mode_enforce_fails(self, tmp_path):
        proc, _ = self._run_step(
            self.apply_step, tmp_path, {"EVAL_RC": "1", "VERIFY_RC": "", "MODE": "enforce"}
        )
        assert proc.returncode == 1
        assert "closure-evidence: FAIL" in proc.stdout


class TestWorkflowF1TriggersAndGuard:
    """Copilot review comment id 4052018483: the gate must re-fire on cross-
    family reviews, and the job `if` must scope the review event to master
    because pull_request_review cannot carry a `branches` filter."""

    def _workflow_text(self):
        return WORKFLOW_REL.read_text(encoding="utf-8", errors="replace")

    def test_pull_request_review_trigger_present(self):
        keys = dict(extract_top_keys(self._workflow_text()))
        on_block = keys["on"]
        on_subs = [name for name, _ in extract_subkeys(on_block)]
        assert "pull_request_review" in on_subs
        # pull_request must also still be present (existing contract).
        assert "pull_request" in on_subs

    def test_pull_request_review_includes_submitted(self):
        keys = dict(extract_top_keys(self._workflow_text()))
        on_subs = dict(extract_subkeys(keys["on"]))
        types = [
            t.lstrip("- ").strip().strip("\"'")
            for t in on_subs["pull_request_review"]
            if t.lstrip().startswith("- ")
        ]
        assert "submitted" in types
        assert "edited" in types
        assert "dismissed" in types

    def test_job_if_blocks_review_on_non_master_base(self):
        """The job `if:` must scope pull_request_review events to master
        because that event cannot carry a `branches:` filter (Copilot F1)."""
        text = self._workflow_text()
        # Find the `if:` line(s) on the closure-evidence-gate job and the
        # scalar value they hold (which may span multiple lines).
        in_job = False
        if_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("closure-evidence-gate:"):
                in_job = True
                continue
            if in_job and stripped.startswith("steps:"):
                break
            if in_job and stripped.startswith("if: |"):
                if_lines = [stripped[5:].lstrip()]
                continue
            if in_job and if_lines:
                # Continuation lines of the `if:` scalar are more-indented than
                # the key; stop at any sibling key (no leading space at the
                # job-key indent).
                if line and not line[0].isspace():
                    if_lines = []
                    continue
                if_lines.append(stripped)
        joined = "\n".join(if_lines)
        assert "pull_request_review" in joined, (
            f"job `if:` does not branch on pull_request_review (Copilot F1): {joined!r}"
        )
        assert "master" in joined, (
            f"job `if:` does not scope pull_request_review events to master: {joined!r}"
        )

    def test_pull_request_trigger_still_branches_master(self):
        """Regression: the original pull_request block keeps its branches:
        [master] filter (don't lose existing scoping)."""
        text = self._workflow_text()
        keys = dict(extract_top_keys(text))
        pr_block = dict(extract_subkeys(keys["on"]))["pull_request"]
        pr_lines = [l.strip() for l in pr_block]
        joined = "\n".join(pr_lines)
        assert "master" in joined
        # The explicit list of activity types must still include synchronize
        # (asserted by C1 frozen driver too, so this is belt-and-braces).
        assert "synchronize" in joined

    def test_no_pull_request_review_branches_filter_in_yaml(self):
        """pull_request_review does NOT support `branches:` in YAML on
        GitHub; this is the whole reason the scoping had to move into the
        job `if`. Pin that the workflow does NOT add a `branches:` key under
        pull_request_review (which would be silently ignored)."""
        text = self._workflow_text()
        keys = dict(extract_top_keys(text))
        review_block = dict(extract_subkeys(keys["on"]))["pull_request_review"]
        review_lines = "\n".join(l.strip() for l in review_block)
        assert "branches:" not in review_lines, (
            f"pull_request_review cannot carry a branches filter — remove it: {review_lines!r}"
        )
