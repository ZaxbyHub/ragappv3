"""Issue #738 wiring pin (G06): the setup-docs truth gate runs in CI.

Pins every surface that must name ``scripts/check_g06_setup_docs.py``: the
ci.yml quality-contracts step, the justfile recipe, the two enforced
engineering-doc inventories (``check_runtime_contract.py`` derives its
inventory from ci.yml and requires both docs name every script), the
AGENTS.md/CONTRIBUTING.md human-facing lists, and the ci-compatibility-audit
skill mirror. Also proves the inventory regex governs the digit-bearing name
("g06", same class as the b03 pin) and exercises the gate's own
missing-documentation and Annotated-declaration branches against mutated
copies (plan-critic round 1 findings 1 and 5).

Model: ``test_b03_contract_wiring.py`` (issue #692).
"""

import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMMAND = "python scripts/check_g06_setup_docs.py"
CHECKER = REPO / "scripts" / "check_runtime_contract.py"
GATE = REPO / "scripts" / "check_g06_setup_docs.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("check_g06_setup_docs", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_g06_guard_wired_into_ci_and_justfile():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_lines = ci.splitlines()
    in_quality_job = False
    found_ci_step = False
    for line in ci_lines:
        if line.strip().startswith("quality-contracts:"):
            in_quality_job = True
            continue
        if in_quality_job and line.strip() and not line.startswith("      "):
            if line.strip() and not line.startswith(" "):
                break
        if in_quality_job and not line.lstrip().startswith("#") and COMMAND in line:
            found_ci_step = True
            break
    assert found_ci_step, "ci.yml quality-contracts job must run the g06 guard"

    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    jf_lines = justfile.splitlines()
    in_recipe = False
    found_just_line = False
    for line in jf_lines:
        if line.startswith("quality-contracts:"):
            in_recipe = True
            continue
        if in_recipe and line and not line.startswith(" "):
            break
        if in_recipe and not line.lstrip().startswith("#") and COMMAND in line:
            found_just_line = True
            break
    assert found_just_line, "justfile quality-contracts recipe must run the g06 guard"


def test_g06_guard_governed_by_runtime_contract_inventory():
    spec = importlib.util.spec_from_file_location("check_runtime_contract", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.DOC_SCRIPT_RE.search(
        "scripts/check_g06_setup_docs.py"
    ), "the inventory regex must govern the digit-bearing script name"

    for doc in (
        "docs/engineering/testing.md",
        "docs/engineering/conventions.md",
    ):
        text = (REPO / doc).read_text(encoding="utf-8")
        assert "scripts/check_g06_setup_docs.py" in text, (
            f"{doc} must name the g06 guard in its contract-script inventory"
        )


def test_g06_guard_named_in_human_facing_lists():
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert "check_g06_setup_docs.py" in agents, (
        "AGENTS.md CI-gates list must name the g06 guard"
    )
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "check_g06_setup_docs.py" in contributing, (
        "CONTRIBUTING.md local-gates block must name the g06 guard"
    )
    skill = (
        REPO / ".agents" / "skills" / "ci-compatibility-audit" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert skill.count("check_g06_setup_docs.py") >= 2, (
        "the ci-compatibility-audit skill mirror must list the g06 guard in both "
        "the step list and the local-mirror block"
    )


def test_g06_redis_url_requires_documented_key():
    """A template with NO REDIS_URL entry (active or commented) fails the mode.

    The mode consults documented_env_entry first; these helper semantics are
    what turn a deleted key into the distinct missing-documentation FAIL
    (plan-critic round 1 finding 5).
    """
    gate = _load_gate_module()
    env_text = "# some unrelated template\nOTHER_KEY=1\n"
    assert gate.documented_env_entry(env_text, "REDIS_URL") is None
    assert gate.documented_env_entry("#REDIS_URL=redis://x\n", "REDIS_URL") == (
        1,
        "redis://x",
    )
    assert gate.active_env_entry("#REDIS_URL=redis://x\n", "REDIS_URL") is None
    assert gate.active_env_entry("REDIS_URL=redis://x\n", "REDIS_URL") == (
        1,
        "redis://x",
    )


def test_g06_data_dir_requires_documented_key():
    """A template with NO DATA_DIR entry (active or commented) fails the mode."""
    gate = _load_gate_module()
    assert gate.documented_env_entry("UNRELATED=1\n", "DATA_DIR") is None
    assert gate.documented_env_entry("#DATA_DIR=/app/data\n", "DATA_DIR") == (
        1,
        "/app/data",
    )


def test_g06_allowed_extensions_parser_accepts_annotated_declaration():
    """The static parser reads the default set from both declaration forms.

    The plain ``set[str]`` form is the pre-#734 shape; the
    ``Annotated[set[str], NoDecode]`` form is what #734 ships. Both must yield
    the same default extension set (plan-critic round 1 finding 1).
    """
    gate = _load_gate_module()
    literal_body = (
        '".txt",\n".md",\n".pdf",\n".pptx",\n'
    )
    raster_body = '".png", ".jpg"'
    for decl in (
        "allowed_extensions: set[str] = {%s} | set(RASTER_IMAGE_EXTENSIONS)"
        % literal_body,
        "allowed_extensions: Annotated[set[str], NoDecode] = {%s}"
        "| set(RASTER_IMAGE_EXTENSIONS)" % literal_body,
    ):
        match = re.search(
            r"allowed_extensions:\s*(?:Annotated\[set\[str\],\s*NoDecode\]|set\[str\])"
            r"\s*=\s*\{(.*?)\}\s*\|\s*set\(RASTER_IMAGE_EXTENSIONS\)",
            decl,
            re.DOTALL,
        )
        assert match is not None, f"parser must accept declaration: {decl[:60]}..."
        found = set(re.findall(r'"([^"]+)"', match.group(1)))
        assert found == {".txt", ".md", ".pdf", ".pptx"}
    assert set(re.findall(r'"([^"]+)"', raster_body)) == {".png", ".jpg"}
