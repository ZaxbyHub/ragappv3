"""Issue #258 acceptance check — skill-sync adapter completeness (TOOL-001).

``scripts/sync_skills.py`` mirrors repo-specific skills across the three
runner trees. For adapter skills (ADAPTER_SKILLS, currently
``codebase-review-swarm``) one tree holds the canonical protocol and the other
trees must hold thin adapters pointing at it. The audited defect: the
``len(present_in) < 2`` guard treats a canonical copy with zero or one adapter
as acceptable, so a silently-missing runner adapter never fails ``--check``.

These nodes import the real ``sync_skills`` module and drive its
``check_drift()`` against temp skill-tree fixtures (canonical + adapters built
under ``tmp_path``), patching only ``sync_skills.ROOT`` — the module reads that
global at call time, so no repo files are touched. Each node prints an
``AC10 CHECK: PASS`` / ``AC10 CHECK: FAIL`` sentinel under ``-s``.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import sync_skills  # noqa: E402

SKILL = "codebase-review-swarm"
CANONICAL_TREE = ".opencode/skills"
ADAPTER_TREES = (".agents/skills", ".claude/skills")

CANONICAL_BODY = "\n".join(
    f"canonical protocol step {index}: instructions..." for index in range(1, 21)
) + "\n"
ADAPTER_BODY = (
    "Adapter for runner tree.\n"
    f"Canonical protocol lives in {sync_skills.ADAPTER_POINTER_PREFIX}{SKILL}/\n"
    "Do not edit here; sync from the canonical tree.\n"
)


def _write_skill(root: Path, tree: str, body: str) -> None:
    skill_dir = root / tree / SKILL
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _drift_for(root: Path, monkeypatch) -> list[str]:
    monkeypatch.setattr(sync_skills, "ROOT", root)
    return sync_skills.check_drift()


def test_ac10_missing_one_adapter_is_reported(monkeypatch, tmp_path):
    """Canonical + exactly ONE adapter must report the missing third tree."""
    try:
        _write_skill(tmp_path, CANONICAL_TREE, CANONICAL_BODY)
        _write_skill(tmp_path, ADAPTER_TREES[0], ADAPTER_BODY)
        findings = _drift_for(tmp_path, monkeypatch)
        print(f"findings: {findings}")
        missing = [
            finding
            for finding in findings
            if "missing" in finding and ADAPTER_TREES[1] in finding
        ]
        assert missing, (
            f"skill-sync check_drift() must report adapter {SKILL} missing in "
            f"{ADAPTER_TREES[1]} when only one adapter exists; got {findings!r}"
        )
    except Exception:
        print("AC10 CHECK: FAIL")
        raise
    print("AC10 CHECK: PASS")


def test_ac10_canonical_with_zero_adapters_is_reported(monkeypatch, tmp_path):
    """Canonical copy with NO adapters must not pass the check silently."""
    try:
        _write_skill(tmp_path, CANONICAL_TREE, CANONICAL_BODY)
        findings = _drift_for(tmp_path, monkeypatch)
        print(f"findings: {findings}")
        assert findings and all(
            any(tree in finding for tree in ADAPTER_TREES) for finding in findings
        ) and len(findings) >= 1, (
            "skill-sync check_drift() must report both missing adapter trees for "
            f"canonical-only {SKILL}; got {findings!r}"
        )
    except Exception:
        print("AC10 CHECK: FAIL")
        raise
    print("AC10 CHECK: PASS")


def test_ac10_valid_canonical_plus_both_adapters_passes(monkeypatch, tmp_path):
    """Control: a valid three-tree adapter layout produces no findings.

    Keeps the discriminating nodes honest — the repaired check must still
    accept the correct state (all three valid copies pass, per the issue).
    """
    try:
        _write_skill(tmp_path, CANONICAL_TREE, CANONICAL_BODY)
        for tree in ADAPTER_TREES:
            _write_skill(tmp_path, tree, ADAPTER_BODY)
        findings = _drift_for(tmp_path, monkeypatch)
        print(f"findings: {findings}")
        assert findings == [], (
            f"check_drift() must accept a valid canonical+2-adapter layout; "
            f"got {findings!r}"
        )
    except Exception:
        print("AC10 CHECK: FAIL")
        raise
    print("AC10 CHECK: PASS")
