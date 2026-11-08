"""Gate-table mirror check for the ci-fix-monitor skill (#1063).

Issue #1063's production change is a docs row: the quality-contracts row
of the CI-surface table in ``.agents/skills/ci-fix-monitor/SKILL.md`` must
name every ``scripts/check_*.py`` gate that ``.github/workflows/ci.yml``
runs. Nothing else enforces this — ``check_runtime_contract.py`` governs
the script inventory only against ``docs/engineering/testing.md`` and
``docs/engineering/conventions.md``, never skill tables (scope
intentionally unchanged per #1063) — so the table drifted to a stale
five-script subset when gates were added, and would drift again silently.

This module is the pin the issue requires:

* every ``scripts/check_*.py`` name appearing in ci.yml also appears in
  the canonical skill (the #1063 AC1 discriminating predicate: the
  ci.yml-minus-skill set diff must be empty);
* the quality-contracts table row itself still exists, so the set check
  cannot pass vacuously by deleting the row.

At the pre-fix base the skill names 5 of the 18 ci.yml gate scripts, so
the set-diff assertion fails there and passes at head — this file is the
PR's executing regression test for AC1 (#1063).
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Same shape as the AC1 argv regex: scripts/check_<lowercase>.py paths.
_GATE_SCRIPT_RE = re.compile(r"scripts/check_[a-z0-9_]+\.py")

SKILL_PATH = (".agents", "skills", "ci-fix-monitor", "SKILL.md")
CI_PATH = (".github", "workflows", "ci.yml")
_TABLE_ROW_MARKER = "| **Quality contracts** |"


def _read(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


def test_skill_table_names_every_ci_gate_script():
    """AC1: every ci.yml check_*.py gate must be named in the skill table.

    The skill is the operator's map of the CI surface; a gate the table
    omits is a gate that can go red with no diagnosis guidance. Frozen
    contract: the set of ``scripts/check_*.py`` names in ci.yml is a
    subset of the names in the canonical ci-fix-monitor SKILL.md.
    """
    ci_text = _read(*CI_PATH)
    skill_text = _read(*SKILL_PATH)
    ci_gates = set(_GATE_SCRIPT_RE.findall(ci_text))
    skill_gates = set(_GATE_SCRIPT_RE.findall(skill_text))
    assert ci_gates, "no check_*.py gate scripts parsed from ci.yml"
    assert skill_gates, "no check_*.py gate scripts parsed from the skill"
    missing = sorted(ci_gates - skill_gates)
    assert missing == [], (
        "ci-fix-monitor SKILL.md quality-contracts table is stale: "
        "ci.yml gate scripts absent from the skill (refresh the table "
        f"row to the full CI-ordered set): {missing}"
    )


def test_quality_contracts_table_row_exists():
    """Guard: the set check targets a table row, not the file at large.

    Deleting the quality-contracts row outright (or renaming its bold
    cell) must fail loudly here rather than let the subset assertion
    pass on incidental script mentions elsewhere in the skill.
    """
    skill_text = _read(*SKILL_PATH)
    assert _TABLE_ROW_MARKER in skill_text, (
        "ci-fix-monitor SKILL.md lost the '| **Quality contracts** |' "
        "table row that the #1063 gate-set pin is written against"
    )
