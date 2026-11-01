"""PR #1045 feedback (PRR-007d/e): pins the two run_bandit/check_sast
branches the first wave left untested.

- run_bandit.update_baseline's bandit-``errors`` early-fail (the twin of
  gated_scan's, at scripts/run_bandit.py:275-283).
- check_sast_baseline's ALLOW_EXPANSION=1 permitted-growth branch (env read
  at module import, so the test env is set BEFORE the module loads).
- check_sast_baseline's GITHUB_ACTIONS git-fault fail-closed leg (PRR-008)
  and the ST-06 None-vs-returncode distinction (git broken != no baseline).
"""

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load_script(filename: str, key: str):
    spec = importlib.util.spec_from_file_location(key, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_update_baseline_fails_on_bandit_parse_errors(tmp_path, monkeypatch, capsys):
    """The regeneration path must refuse to write a baseline while bandit
    reports unparseable files (they would be silently skipped)."""
    mod = _load_script("run_bandit.py", "run_bandit_hardening")
    baseline = tmp_path / "bandit-baseline.json"
    baseline.write_text(json.dumps({"results": []}), encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE", baseline)

    def fake_run(targets, extra):
        return 1, {
            "results": [],
            "errors": [{"filename": "backend/app/x.py", "reason": "syntax error"}],
        }, ""

    monkeypatch.setattr(mod, "_run_bandit_json", fake_run)
    rc = mod.update_baseline()
    assert rc == 1
    combined = capsys.readouterr().err
    assert "unparseable" in combined and "backend/app/x.py" in combined
    # The baseline file must be untouched (refused before writing).
    assert json.loads(baseline.read_text(encoding="utf-8")) == {"results": []}


def test_update_baseline_succeeds_without_errors(tmp_path, monkeypatch):
    """Happy-path guard: no errors array -> the baseline IS written."""
    mod = _load_script("run_bandit.py", "run_bandit_hardening2")
    baseline = tmp_path / "bandit-baseline.json"
    baseline.write_text(json.dumps({"results": []}), encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE", baseline)
    monkeypatch.setattr(mod, "ROOT", tmp_path)  # the success message relativizes to ROOT

    def fake_run(targets, extra):
        return 1, {"results": [], "errors": []}, ""

    monkeypatch.setattr(mod, "_run_bandit_json", fake_run)
    rc = mod.update_baseline()
    assert rc == 0
    written = json.loads(baseline.read_text(encoding="utf-8"))
    assert "results" in written  # normalized baseline written


def _fresh_sast_module_with_expansion_allowed():
    """Load check_sast_baseline with SAST_ALLOW_BASELINE_EXPANSION=1 already
    in the environment (the flag is captured at module import)."""
    env_key = "SAST_ALLOW_BASELINE_EXPANSION"
    old = os.environ.get(env_key)
    os.environ[env_key] = "1"
    try:
        return _load_script("check_sast_baseline.py", "check_sast_baseline_exp")
    finally:
        if old is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = old


def test_baseline_expansion_allowed_with_flag(tmp_path, monkeypatch):
    """SAST_ALLOW_BASELINE_EXPANSION=1 permits a growing baseline (the
    deliberate-expansion branch the first wave never exercised)."""
    mod = _fresh_sast_module_with_expansion_allowed()
    assert mod.ALLOW_EXPANSION is True, "module must capture the env at import"
    baseline_rel = "backend/security/bandit-baseline.json"
    prev = {"results": [{"test_id": "B101", "filename": "a.py", "line_number": 1}]}
    bigger = {"results": prev["results"] + [
        {"test_id": "B101", "filename": "a.py", "line_number": 2}
    ]}
    baseline_path = tmp_path / "bandit-baseline.json"
    baseline_path.write_text(json.dumps(bigger), encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE", baseline_path)
    monkeypatch.setattr(mod, "changed_files", lambda: [baseline_rel])

    def fake_gate_git_run_git(root, *args, **kwargs):
        if args and args[0] == "show":
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(prev))
        if args and args[0] == "merge-base":
            return types.SimpleNamespace(returncode=0, stdout="0" * 40 + "\n")
        return types.SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(mod._gate_git, "run_git", fake_gate_git_run_git)
    assert mod.check_baseline_expansion([baseline_rel]) is True


def test_git_broken_fails_closed_in_ci(tmp_path, monkeypatch, capsys):
    """PRR-008: under GITHUB_ACTIONS a git instrument fault must FAIL the
    gate (exit 1) instead of printing 'all checks passed'."""
    mod = _load_script("check_sast_baseline.py", "check_sast_baseline_ci")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(
        mod._gate_git, "git_lines", lambda root, *args, **kw: None
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rc = mod.main()
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc == 1
    assert "all checks passed" not in combined
    assert "could not" in combined


def test_git_broken_degrades_locally(tmp_path, monkeypatch, capsys):
    """Locally (no GITHUB_ACTIONS) the same fault keeps the skip-with-note
    behavior — and must NOT claim 'all checks passed' for the C-002 leg's
    sake without saying why (note is printed)."""
    mod = _load_script("check_sast_baseline.py", "check_sast_baseline_local")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(
        mod._gate_git, "git_lines", lambda root, *args, **kw: None
    )
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    rc = mod.main()
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc == 0  # local degrade stays a pass, with the note...
    assert "note" in combined and "git" in combined  # ...which is printed


def test_baseline_show_fault_is_not_seeding(tmp_path, monkeypatch, capsys):
    """ST-06: a broken `git show` during the prior-baseline lookup must NOT
    take the 'initial baseline seeded — allowed' branch."""
    mod = _load_script("check_sast_baseline.py", "check_sast_baseline_seed")
    baseline_rel = "backend/security/bandit-baseline.json"
    baseline_path = tmp_path / "b.json"
    bigger = {"results": [
        {"test_id": "B101", "filename": "a.py", "line_number": 1},
        {"test_id": "B101", "filename": "a.py", "line_number": 2},
    ]}
    baseline_path.write_text(json.dumps(bigger), encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE", baseline_path)
    monkeypatch.setattr(mod, "changed_files", lambda: [baseline_rel])

    def broken_git(root, *args, **kwargs):
        return None  # git broken for every call

    monkeypatch.setattr(mod._gate_git, "git_lines", lambda root, *a, **k: None)
    monkeypatch.setattr(mod._gate_git, "run_git", broken_git)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    ok = mod.check_baseline_expansion([baseline_rel])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert ok is False, "a git fault must fail the gate, not 'seed' it"
    assert "initial baseline seeded" not in combined
