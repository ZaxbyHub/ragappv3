"""Issue #740 (AC15): scripts/check_pr_scope_drift.py is test-pinned.

Pins the happy path, the auth-drift failure, and the REAL auth-runtime
prefix coverage (issue #740 round 1: ``backend/app/services/auth`` is a
live prefix covering auth_service.py and authz_policy.py — the fix keeps
it and adds backend/app/api/deps.py).
"""

import contextlib
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_pr_scope_drift.py"




@contextlib.contextmanager
def _no_bytecode():
    """Keep spec_from_file_location loads from writing scripts/__pycache__
    into the repository working tree (the frozen siblings do the same)."""
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = old

def _load():
    spec = importlib.util.spec_from_file_location("check_pr_scope_drift_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with _no_bytecode():
        spec.loader.exec_module(mod)
    return mod


def test_scope_drift_passes_when_auth_runtime_changes(monkeypatch):
    """Happy path: an auth test paired with auth runtime returns 0."""
    mod = _load()
    monkeypatch.setattr(
        mod,
        "changed_files",
        lambda: [
            "backend/tests/test_auth_x.py",
            "backend/app/api/routes/auth.py",
        ],
    )
    assert mod.main() == 0


def test_scope_drift_flags_auth_test_without_runtime(monkeypatch):
    """Seeded violation: auth test alone returns 1."""
    mod = _load()
    monkeypatch.setattr(mod, "changed_files", lambda: ["backend/tests/test_auth_x.py"])
    assert mod.main() == 1


def test_scope_drift_counts_api_deps_as_auth_runtime(monkeypatch):
    """#740-AC5: backend/app/api/deps.py (the real dependency module) counts."""
    mod = _load()
    monkeypatch.setattr(
        mod,
        "changed_files",
        lambda: ["backend/tests/test_auth_x.py", "backend/app/api/deps.py"],
    )
    assert mod.main() == 0


def test_scope_drift_keeps_services_auth_prefix_coverage(monkeypatch):
    """The ``backend/app/services/auth`` PREFIX must keep covering the real
    auth service modules (round-1 critic finding: removing it as a "dead
    directory" would flag legitimate auth PRs)."""
    mod = _load()
    for runtime in (
        "backend/app/services/auth_service.py",
        "backend/app/services/authz_policy.py",
    ):
        monkeypatch.setattr(
            mod,
            "changed_files",
            lambda r=runtime: ["backend/tests/test_auth_x.py", r],
        )
        assert mod.main() == 0, f"{runtime} must count as auth runtime"


def test_scope_drift_removed_dead_prefixes(monkeypatch):
    """The two dead entries the fix removed must no longer be named: a file
    under backend/app/models/auth* (which does not exist) cannot be required
    to satisfy the gate, and the tuple must not carry phantom paths."""
    mod = _load()
    monkeypatch.setattr(
        mod,
        "changed_files",
        lambda: ["backend/tests/test_auth_x.py", "backend/app/api/deps.py"],
    )
    mod.main()  # sanity: passes via the real module
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"backend/app/deps.py"' not in source
    assert '"backend/app/models/auth"' not in source


def test_scope_drift_git_fault_fails_closed_in_ci(tmp_path, monkeypatch, capsys):
    """PRR-008: under GITHUB_ACTIONS a git instrument fault must fail the
    gate (main() -> 1) instead of printing 'all checks passed'."""
    mod = _load()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod._gate_git, "git_lines", lambda root, *args, **kw: None)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rc = mod.main()
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc == 1
    assert "all checks passed" not in combined
