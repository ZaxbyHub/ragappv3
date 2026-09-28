"""Issue #692 wiring pin (B03): the upload-migration timeout contract runs in CI.

The digit-bearing script name is invisible to the OLD
``check_runtime_contract.py`` doc-inventory regex (``[a-z_]+`` cannot match
"b03"), so nothing would notice the gate being dropped from CI or the docs.
This test pins all four surfaces: the ci.yml step, the justfile recipe, both
documented inventories, and — via the checker's own loaded module — that the
inventory regex actually governs the script name (issue #692, plan-critic
finding 1; model: test_settings_consumers_gate.py::test_t7_census_wired...).
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMMAND = "python scripts/check_b03_upload_migration_timeout.py"
CHECKER = REPO / "scripts" / "check_runtime_contract.py"


def test_b03_guard_wired_into_ci_and_justfile():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_lines = ci.splitlines()
    in_quality_job = False
    found_ci_step = False
    for line in ci_lines:
        if line.startswith("  quality-contracts:"):
            in_quality_job = True
            continue
        if in_quality_job and line.startswith("  ") and not line.startswith("    "):
            break  # next top-level job key ends the block
        if in_quality_job and not line.lstrip().startswith("#") and COMMAND in line:
            found_ci_step = True
            break
    assert found_ci_step, "ci.yml quality-contracts job must run the b03 guard"

    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    jf_lines = justfile.splitlines()
    in_recipe = False
    found_just_line = False
    for line in jf_lines:
        if line.startswith("quality-contracts:"):
            in_recipe = True
            continue
        if in_recipe and line and not line.startswith(" "):
            break  # next recipe ends the block
        if in_recipe and not line.lstrip().startswith("#") and COMMAND in line:
            found_just_line = True
            break
    assert found_just_line, "justfile quality-contracts recipe must run the b03 guard"


def test_b03_guard_governed_by_runtime_contract_inventory():
    spec = importlib.util.spec_from_file_location("check_runtime_contract", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.DOC_SCRIPT_RE.search(
        "scripts/check_b03_upload_migration_timeout.py"
    ), "the inventory regex must govern the digit-bearing script name"

    for doc in (
        "docs/engineering/testing.md",
        "docs/engineering/conventions.md",
    ):
        text = (REPO / doc).read_text(encoding="utf-8")
        assert "scripts/check_b03_upload_migration_timeout.py" in text, (
            f"{doc} must name the b03 guard in its contract-script inventory"
        )
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert "check_b03_upload_migration_timeout.py" in agents, (
        "AGENTS.md must name the b03 guard in its contract-script list"
    )


if __name__ == "__main__":
    sys.exit(
        0
        if (
            test_b03_guard_wired_into_ci_and_justfile() is None
            and test_b03_guard_governed_by_runtime_contract_inventory() is None
        )
        else 1
    )
