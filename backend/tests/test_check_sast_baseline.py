"""Issue #740 (AC15): scripts/check_sast_baseline.py is test-pinned.

Pins the three integrity checks: C-005 (the sast job binding — including the
misplaced-step negative), C-003 (TARGETS scope), C-002 (baseline expansion
gate). The gate had zero test references at the #740 base.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_sast_baseline.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_sast_baseline_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sast_c005_passes_on_repo():
    """Happy path: the real ci.yml satisfies C-005 (run_bandit inside sast)."""
    mod = _load()
    assert mod.check_workflow_integrity() is True


def test_sast_c005_rejects_misplaced_run_bandit(tmp_path):
    """Seeded violation (#740-AC1): a run_bandit.py step outside the sast job
    while the sast job runs something else must FAIL the binding."""
    mod = _load()
    fake = tmp_path / "ci.yml"
    fake.write_text(
        "jobs:\n"
        "  lint:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: scan\n"
        "        run: python scripts/run_bandit.py\n"
        "  sast:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: scan\n"
        "        run: echo pass\n",
        encoding="utf-8",
    )
    monkeypatch_target = mod.CI_FILE
    assert monkeypatch_target  # the module-level path exists
    mod.CI_FILE = fake
    try:
        assert mod.check_workflow_integrity() is False
    finally:
        mod.CI_FILE = monkeypatch_target


def test_sast_c005_rejects_update_baseline_mode(tmp_path):
    """The always-exits-0 regeneration mode must not satisfy C-005 inside the
    sast job either."""
    mod = _load()
    fake = tmp_path / "ci.yml"
    fake.write_text(
        "jobs:\n"
        "  sast:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: scan\n"
        "        run: python scripts/run_bandit.py --update-baseline\n",
        encoding="utf-8",
    )
    original = mod.CI_FILE
    mod.CI_FILE = fake
    try:
        assert mod.check_workflow_integrity() is False
    finally:
        mod.CI_FILE = original


def test_sast_c003_scope_integrity_on_repo():
    """Happy path: run_bandit.py TARGETS equals the expected set."""
    mod = _load()
    assert mod.check_scope_integrity() is True


def test_sast_c003_rejects_shrunk_scope(tmp_path, monkeypatch):
    """Seeded violation: a shrunken TARGETS tuple must fail C-003."""
    mod = _load()
    shrunken = tmp_path / "run_bandit.py"
    shrunken.write_text("TARGETS = ('backend/app',)\n", encoding="utf-8")
    original = mod.RUN_BANDIT
    mod.RUN_BANDIT = shrunken
    try:
        assert mod.check_scope_integrity() is False
    finally:
        mod.RUN_BANDIT = original


def test_sast_c002_blocks_baseline_growth(tmp_path, monkeypatch):
    """Seeded violation (#740): a baseline in the PR diff growing by one
    finding without SAST_ALLOW_BASELINE_EXPANSION must fail C-002."""
    import types

    mod = _load()
    monkeypatch.delenv("SAST_ALLOW_BASELINE_EXPANSION", raising=False)
    baseline_rel = "backend/security/bandit-baseline.json"

    prev = {"results": [{"test_id": "B101", "filename": "a.py", "line_number": 1}]}
    bigger = {"results": prev["results"] + [
        {"test_id": "B101", "filename": "a.py", "line_number": 2}
    ]}
    baseline_path = tmp_path / "bandit-baseline.json"
    baseline_path.write_text(json.dumps(bigger), encoding="utf-8")

    monkeypatch.setattr(mod, "BASELINE", baseline_path)
    monkeypatch.setattr(mod, "changed_files", lambda: [baseline_rel])
    # The prior-baseline lookup: origin/master resolves to a merge-base whose
    # `git show` returns the SMALLER baseline (faking the base-branch state).
    def fake_gate_git_run_git(root, *args, **kwargs):
        if args and args[0] == "show":
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(prev))
        if args and args[0] == "merge-base":
            return types.SimpleNamespace(returncode=0, stdout="0" * 40 + "\n")
        return types.SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(mod._gate_git, "run_git", fake_gate_git_run_git)

    assert mod.check_baseline_expansion([baseline_rel]) is False
