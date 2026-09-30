"""Wiring pin for the HTTP 500 detail-hygiene gate (issue #686, AC12).

The gate (scripts/check_a04_http500_detail_hygiene.py) only protects the
routes if it actually runs: these tests pin its presence in the CI
quality-contracts job and the justfile recipe (modeled on
test_settings_consumers_gate.py::test_t7_census_wired_into_ci_and_justfile)
so the step cannot be silently dropped.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

COMMAND = "python scripts/check_a04_http500_detail_hygiene.py"


def test_gate_wired_into_ci_quality_contracts_job():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    in_quality_job = False
    for line in ci.splitlines():
        if line.startswith("  quality-contracts:"):
            in_quality_job = True
            continue
        if in_quality_job and line.startswith("  ") and not line.startswith("    "):
            break  # next top-level job key ends the block
        if in_quality_job and not line.lstrip().startswith("#") and COMMAND in line:
            return
    raise AssertionError("ci.yml quality-contracts job must run the hygiene gate")


def test_gate_wired_into_justfile_quality_contracts_recipe():
    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    in_recipe = False
    for line in justfile.splitlines():
        if line.startswith("quality-contracts:"):
            in_recipe = True
            continue
        if in_recipe and line and not line.startswith(" "):
            break  # next recipe ends the block
        if in_recipe and not line.lstrip().startswith("#") and COMMAND in line:
            return
    raise AssertionError("justfile quality-contracts recipe must run the hygiene gate")
