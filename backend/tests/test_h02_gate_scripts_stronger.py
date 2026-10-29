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
    gate message (its own prefix) and an exit code in {0, 1} — not silence."""
    import sys as _sys

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
    assert _sys.modules is not None  # keep import used
