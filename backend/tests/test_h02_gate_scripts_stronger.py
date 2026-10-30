"""Issue #740 layered coverage (plan-critic round 1, finding 9): supplements
the frozen C20/C22 legs, which can pass on weaker behavior than they name.

- C22 supplement: the frozen check rejects C-QUOTED output but an
  all-empty result also passes it — this module requires the RAW
  ``docs/résumé.env`` path to be PRESENT in every helper's result.
- C20 supplement: the frozen check rejects raised exceptions but silent
  success also passes it — this module requires each main() under
  missing-git to print a gate-prefixed message and exit in {0, 1}.

NON-frozen on purpose (the frozen files are byte-locked by the checkpoint).
"""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load(filename: str, key: str):
    spec = importlib.util.spec_from_file_location(key, SCRIPTS / f"{filename}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_unicode_repo(tmp_path: Path) -> str:
    """A two-commit repo whose SECOND commit adds a non-ASCII path; returns
    the first commit's sha (a resolvable diff base)."""
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True,
            capture_output=True, text=True,
        )

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("config", "core.quotePath", "true")
    (tmp_path / "README.md").write_text("seed", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "seed")
    base = git("rev-parse", "HEAD").stdout.strip()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "résumé.env").write_text("x", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "add unicode path")
    return base


@pytest.mark.parametrize(
    "script_name", ["check_secretscan", "check_pr_scope_drift", "check_sast_baseline"]
)
def test_git_listing_returns_raw_unicode_path(tmp_path, monkeypatch, script_name):
    """C22 supplement: every listing helper must return the RAW path (not
    merely fail to return a quoted one — an empty list would also do that)."""
    base = _seed_unicode_repo(tmp_path)
    mod = _load(script_name, script_name + "_raw")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    if script_name == "check_secretscan":
        result = mod.tracked_files()
    else:
        monkeypatch.setenv("PR_SCOPE_DRIFT_BASE", base)
        result = mod.changed_files()
    assert result, f"{script_name} returned an empty listing (vacuous pass)"
    assert any(path.endswith("docs/résumé.env") for path in result), result


@pytest.mark.parametrize(
    "script_name", ["check_sast_baseline", "check_secretscan"]
)
def test_missing_git_ends_with_gate_message(monkeypatch, capsys, script_name):
    """C20 supplement: under a missing git binary each main() must END with a
    gate message (its own prefix) and an exit code in {0, 1} — not silence.
    (Under GITHUB_ACTIONS the sast gate now fails closed — the message leg
    holds either way.)"""
    mod = _load(script_name, script_name + "_msg")
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git":
            raise FileNotFoundError("git not on PATH")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        rc = mod.main()
    except FileNotFoundError:  # pragma: no cover - the bug this pins out
        pytest.fail(f"{script_name}.main() raised under missing git")
    out = capsys.readouterr()
    combined = out.out + out.err
    assert rc in (0, 1), f"{script_name} rc={rc}"
    assert re.search(
        r"(sast-baseline|secretscan):", combined
    ), f"{script_name} ended without a gate message: {combined!r}"


def test_walk_prune_keeps_a_positive_control(tmp_path, monkeypatch):
    """AC16 supplement (PRR-007c): the frozen C16 check asserts pruned trees
    are ABSENT; an on_disk_files() that returned NOTHING at all would also
    pass it. This control requires the non-pruned seeded file to be PRESENT."""
    mod = _load("check_secretscan", "check_secretscan_ctrl")
    (tmp_path / "frontend" / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "frontend" / "node_modules" / "pkg" / "a.js").write_text("x", encoding="utf-8")
    (tmp_path / "backend" / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / "backend" / ".venv" / "lib" / "b.py").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    files = mod.on_disk_files()
    assert "notes.txt" in files, (
        f"the non-pruned control file must survive the walk (an empty result "
        f"would make the absence assertions vacuous); got {files!r}"
    )
    assert not any("node_modules" in f or ".venv" in f for f in files)


def test_scope_drift_change_detection_makes_git_calls(tmp_path, monkeypatch):
    """AC21 supplement (PRR-007f): the frozen timeout census passes when a
    script makes NO git call at all. This control requires check_pr_scope_
    drift's git path to actually run (its recorded calls must be non-empty)."""
    calls: list[dict] = []
    real_run = subprocess.run

    def recording_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git":
            calls.append({"timeout": kwargs.get("timeout")})
        return real_run(cmd, *args, **kwargs)

    mod = _load("check_pr_scope_drift", "check_pr_scope_drift_ctrl")
    monkeypatch.setenv("PR_SCOPE_DRIFT_BASE", "origin/master")
    monkeypatch.setattr(subprocess, "run", recording_run)
    mod.changed_files()
    monkeypatch.undo()
    assert calls, "check_pr_scope_drift must issue at least one git call"
    assert all(c["timeout"] is not None for c in calls), (
        f"every recorded git call must carry a finite timeout: {calls!r}"
    )
