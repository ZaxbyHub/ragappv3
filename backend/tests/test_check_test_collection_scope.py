"""Tests for ``scripts/check_test_collection_scope.py`` (issue #656).

The Quality-contracts gate fails when a pytest test file lives outside
``backend/tests/``. Before #656 it enumerated the repository with a raw
filesystem walk, so gitignored local run artifacts (pytest-xdist leftovers
under the gitignored ``backend/data/``) reddened the gate locally with a
remediation message that cannot apply to files git ignores — while CI (a clean
checkout) stayed green. The gate now derives its candidate set from git's view
of the repository (``git ls-files`` plus ``git ls-files --others
--exclude-standard``) and falls back to the original walk, with a one-line
stderr note, whenever git cannot answer.

Contract pinned here (issue #656 acceptance criteria):

* AC1 — a gitignored artifact under ``backend/data/`` is not a violation, and
  the real repo tree is clean.
* AC2 — an untracked, not-ignored ``test_*.py`` outside ``backend/tests/``
  still fails the gate and is named.
* AC3 — a tracked ``test_*.py`` outside ``backend/tests/`` still fails the gate
  and is named.
* AC4 — degraded mode (git unavailable) announces itself and keeps flagging
  real on-disk test files.
* AC5 — this file passes (it is the guard's own regression suite).

Design pins from the trace's adjudications, each covered by a test:

* tracked files under hidden directories stay exempt (documented carve-out;
  keeps CI green on the checked-in ``.opencode/...`` skill-helper test).
* tracked files deleted from the working tree are not on-disk violations
  (``git ls-files`` still lists index-only entries).
* NUL-delimited git output is never stripped (git permits leading/trailing
  whitespace in path components).
* ``PRUNE_DIRS`` is degraded-mode-only: a *tracked* ``venv/`` test file is a
  violation in git mode.

Subprocess pattern follows ``backend/tests/test_issue258_build_contracts.py``:
``sys.executable``, captured output, throwaway git repos under ``tmp_path``,
git driven with an explicit cwd. No node is involved (the Backend CI job has no
frontend install).
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import unittest.mock
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "check_test_collection_scope.py"
GATE_REL = Path("scripts") / "check_test_collection_scope.py"

_SUBPROCESS_TIMEOUT_SECONDS = 120
_GIT_IDENTITY = [
    "-c",
    "user.email=test@example.com",
    "-c",
    "user.name=Test",
    "-c",
    "commit.gpgsign=false",
]

# 7-line unittest probe whose only test asserts False — the same shape as the
# real pytest-xdist worker leftovers under backend/data/. The gate only
# pattern-matches filenames, so the body is never executed.
PROBE_PLAIN = (
    "import unittest\n"
    "\n"
    "\n"
    "class TestPlain(unittest.TestCase):\n"
    "    def test_plain(self):\n"
    "        self.assertFalse(True)\n"
)
PROBE_PASS = "def test_probe():\n    assert True\n"


def _run(argv: list[str], cwd: Path, env_extra: dict[str, str] | None = None):
    env = {**os.environ, "PYTHONUTF8": "1"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        env=env,
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], repo)


def _seed_repo(tmp_path: Path, *, gitignore: str | None = None) -> Path:
    """A throwaway git repo (optionally with a .gitignore) under tmp_path."""
    if gitignore is not None:
        (tmp_path / ".gitignore").write_text(gitignore, encoding="utf-8")
    assert _git(tmp_path, "init").returncode == 0
    return tmp_path


def _install_gate(repo: Path) -> Path:
    """Copy the gate under test into a throwaway repo and return its path."""
    dest = repo / GATE_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(GATE, dest)
    return dest


def _write(repo: Path, rel: str, content: str = PROBE_PLAIN) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _commit(repo: Path, rel: str) -> None:
    assert _git(repo, "add", rel).returncode == 0
    assert _git(repo, *_GIT_IDENTITY, "commit", "-m", "seed").returncode == 0


def _run_gate(repo: Path, *, cwd: Path | None = None, git_available: bool = True):
    env_extra = None if git_available else {"PATH": ""}
    return _run([sys.executable, str(repo / GATE_REL)], cwd or repo, env_extra)


def _assert_flags(proc: subprocess.CompletedProcess, *named: str) -> None:
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 1, (
        f"gate exit={proc.returncode}, expected 1; output:\n{combined}"
    )
    for name in named:
        assert name in combined, f"{name!r} not named in gate output:\n{combined}"


# ── AC1: gitignored artifacts are not violations ─────────────────────────────


def test_gitignored_probe_under_backend_data_is_not_a_violation(tmp_path):
    repo = _seed_repo(tmp_path, gitignore="data/\n")
    _write(repo, "backend/data/x/popen-gw0/test_probe_plain.py")
    _install_gate(repo)

    proc = _run_gate(repo)

    assert proc.returncode == 0, (
        f"gitignored artifact must not trip the gate; output:\n"
        f"{(proc.stdout or '') + (proc.stderr or '')}"
    )


def test_existing_backend_tests_tree_is_clean():
    proc = _run([sys.executable, str(GATE)], REPO)
    assert proc.returncode == 0, (
        f"real repo tree must be clean; output:\n"
        f"{(proc.stdout or '') + (proc.stderr or '')}"
    )


# ── AC2/AC3: real uncollected test files still fail the gate ─────────────────


def test_untracked_test_file_outside_backend_tests_fails(tmp_path):
    repo = _seed_repo(tmp_path)
    _write(repo, "test_probe.py", PROBE_PASS)
    _install_gate(repo)

    proc = _run_gate(repo)

    _assert_flags(proc, "test_probe.py")


def test_committed_test_file_outside_backend_tests_fails(tmp_path):
    repo = _seed_repo(tmp_path)
    _write(repo, "test_probe.py", PROBE_PASS)
    _commit(repo, "test_probe.py")
    _install_gate(repo)

    proc = _run_gate(repo)

    _assert_flags(proc, "test_probe.py")


# ── Hidden-tree carve-out (trace adjudication A1) ────────────────────────────


def test_tracked_test_under_hidden_directory_stays_exempt(tmp_path):
    repo = _seed_repo(tmp_path)
    _write(repo, ".opencode/skills/x/tests/test_skill_helpers.py", PROBE_PASS)
    _commit(repo, ".opencode/skills/x/tests/test_skill_helpers.py")
    _install_gate(repo)

    proc = _run_gate(repo)

    assert proc.returncode == 0, (
        "hidden trees are documented non-collection surfaces and must stay "
        f"exempt; output:\n{(proc.stdout or '') + (proc.stderr or '')}"
    )


# ── AC4: degraded mode announces itself and preserves the walk ───────────────


def test_degraded_mode_without_git_announces_and_preserves_walk(tmp_path):
    repo = _seed_repo(tmp_path, gitignore="data/\n")
    _write(repo, "backend/data/x/popen-gw0/test_probe_plain.py")
    _write(repo, "test_probe_untracked.py", PROBE_PASS)
    _write(repo, "test_probe_tracked.py", PROBE_PASS)
    _commit(repo, "test_probe_tracked.py")
    _install_gate(repo)

    proc = _run_gate(repo, git_available=False)
    combined = (proc.stdout or "") + (proc.stderr or "")

    lowered = combined.lower()
    assert "git unavailable" in lowered, f"degraded note missing:\n{combined}"
    assert "falling back" in lowered, f"degraded note missing:\n{combined}"
    assert proc.returncode == 1, f"degraded walk must still fail; output:\n{combined}"
    assert "test_probe_untracked.py" in combined
    assert "test_probe_tracked.py" in combined


def test_git_command_failure_outside_repo_falls_back(tmp_path):
    repo = tmp_path / "not-a-repo"
    repo.mkdir()
    _write(repo, "test_probe.py", PROBE_PASS)
    _install_gate(repo)

    proc = _run_gate(repo)
    combined = (proc.stdout or "") + (proc.stderr or "")

    assert "git unavailable" in combined.lower(), f"note missing:\n{combined}"
    _assert_flags(proc, "test_probe.py")


# ── On-disk parity: index-only (deleted tracked) entries ─────────────────────


def test_deleted_tracked_test_file_is_not_a_violation(tmp_path):
    repo = _seed_repo(tmp_path)
    probe = _write(repo, "test_probe.py", PROBE_PASS)
    _commit(repo, "test_probe.py")
    probe.unlink()
    _install_gate(repo)

    proc = _run_gate(repo)

    assert proc.returncode == 0, (
        "a tracked file deleted from the working tree is not an on-disk "
        f"violation; output:\n{(proc.stdout or '') + (proc.stderr or '')}"
    )


# ── NUL-token fidelity: exotic and whitespace-bearing paths ──────────────────


def test_leading_whitespace_path_component_is_not_corrupted(tmp_path):
    repo = _seed_repo(tmp_path)
    _write(repo, " lead-dir/test_probe.py", PROBE_PASS)
    _install_gate(repo)

    proc = _run_gate(repo)

    _assert_flags(proc, " lead-dir/test_probe.py")


def test_exotic_filename_parsed_through_nul(tmp_path):
    repo = _seed_repo(tmp_path)
    _write(repo, "test_white space.py", PROBE_PASS)
    _install_gate(repo)

    proc = _run_gate(repo)

    _assert_flags(proc, "test_white space.py")


# ── PRUNE_DIRS is degraded-mode-only ─────────────────────────────────────────


def test_tracked_pruned_name_directory_is_flagged_in_git_mode(tmp_path):
    repo = _seed_repo(tmp_path)
    _write(repo, "venv/lib/test_thing.py", PROBE_PASS)
    _commit(repo, "venv/lib/test_thing.py")
    _install_gate(repo)

    proc = _run_gate(repo)

    _assert_flags(proc, "venv/lib/test_thing.py")


# ── Invocation contract: cwd-independent ROOT resolution ─────────────────────


def test_invocation_from_non_root_cwd(tmp_path):
    repo = _seed_repo(tmp_path)
    _write(repo, "test_probe.py", PROBE_PASS)
    _install_gate(repo)
    (repo / "subdir").mkdir()

    proc = _run_gate(repo, cwd=repo / "subdir")

    _assert_flags(proc, "test_probe.py")


# ── Module-level unit tests for the git failure modes ────────────────────────


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("check_test_collection_scope", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_partial_git_command_failure_falls_back(tmp_path):
    module = _load_gate_module()
    repo = _seed_repo(tmp_path)
    _write(repo, "test_probe.py", PROBE_PASS)
    real_run = subprocess.run
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_run(cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")

    with unittest.mock.patch.object(module.subprocess, "run", side_effect=fake_run):
        with pytest.MonkeyPatch.context() as mp:
            import io

            mp.setattr(sys, "stderr", io.StringIO())
            violations = module.find_violations(repo)
            note = sys.stderr.getvalue()
    assert violations == ["test_probe.py"]
    assert "git unavailable" in note.lower()


def test_git_timeout_falls_back(tmp_path, capsys):
    module = _load_gate_module()
    repo = _seed_repo(tmp_path)
    _write(repo, "test_probe.py", PROBE_PASS)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

    with unittest.mock.patch.object(module.subprocess, "run", side_effect=fake_run):
        violations = module.find_violations(repo)
    captured = capsys.readouterr()
    assert violations == ["test_probe.py"]
    assert "git unavailable" in captured.err.lower()
    assert "falling back" in captured.err.lower()


def test_git_paths_returns_none_when_binary_missing(tmp_path):
    module = _load_gate_module()
    repo = _seed_repo(tmp_path)

    with unittest.mock.patch.object(
        module.subprocess, "run", side_effect=FileNotFoundError("git")
    ):
        assert module._git_paths(repo, "ls-files") is None
