"""Wiring pin for the ci-compatibility-audit local-mirror parity (issue #914).

The skill's Local Mirror Commands are the repo's canonical "reproduce the CI
gates locally before pushing" instruction (AGENTS.md: "CI is the source of
truth"). The four enforced inventories (conventions.md / testing.md via
``check_runtime_contract.py``, plus AGENTS.md and CONTRIBUTING.md) were kept
current, but the skill's hand-copied list drifted 11 contract scripts behind
the ``ci.yml`` quality-contracts job, so a local pre-push audit silently
under-ran Quality contracts. These tests pin the parity in BOTH directions:
adding a ``scripts/check_*.py`` step to the job (or removing one) fails here
until the skill's mirror matches, and a stale extra entry in the skill fails
the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
SKILL = REPO / ".agents" / "skills" / "ci-compatibility-audit" / "SKILL.md"
PARITY_TEST_NAME = "test_ci_compatibility_audit_mirror.py"

_SCRIPT = r"scripts/check_[a-z0-9_]+\.py"


def _quality_contracts_block(ci_text: str) -> str:
    """The `quality-contracts:` job body, up to the next top-level job key."""
    lines = ci_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("  quality-contracts:"):
            start = index
            break
    assert start is not None, "ci.yml has no quality-contracts job"
    end = len(lines)
    for index in range(start + 1, len(lines)):
        # A next job key is exactly two spaces of indent followed by a name
        # and a colon; comment lines start with '#' and never end the block.
        if re.match(r"^  [A-Za-z][\w-]*:$", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _ci_contract_scripts(ci_text: str) -> set[str]:
    """Contract scripts the job actually RUNS.

    Only active ``run: python scripts/check_*.py`` step lines count — a
    commented mention (e.g. the ``# scripts/check_l07...`` note above the
    a11y-matrix job) is not a step (plan-critic round 1, MAJOR-2).
    """
    block = _quality_contracts_block(ci_text)
    scripts: set[str] = set()
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = re.match(r"run:\s*python\s+(" + _SCRIPT + r")", stripped)
        if match:
            scripts.add(match.group(1))
    return scripts


def _skill_mirror_scripts(skill_text: str) -> set[str]:
    """Contract scripts enumerated in the skill's Local Mirror Commands block.

    Only lines inside the fenced bash block count (the section's prose may
    reference scripts in sentences or comments without running them).
    """
    lines = skill_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == "## Local Mirror Commands":
            start = index
            break
    assert start is not None, "skill has no Local Mirror Commands section"
    scripts: set[str] = set()
    in_fence = False
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.strip() == "```bash":
            in_fence = True
            continue
        if line.strip().startswith("```") and in_fence:
            in_fence = False
            continue
        if in_fence:
            match = re.match(r"python\s+(" + _SCRIPT + r")", line.strip())
            if match:
                scripts.add(match.group(1))
    return scripts


def test_mirror_matches_the_quality_contracts_job() -> None:
    """The mirror must run exactly the job's contract scripts (issue #914 AC1)."""
    ci_scripts = _ci_contract_scripts(CI.read_text(encoding="utf-8"))
    mirror = _skill_mirror_scripts(SKILL.read_text(encoding="utf-8"))
    assert ci_scripts, "parser found no contract scripts in ci.yml — check the job key"
    missing = sorted(ci_scripts - mirror)
    extra = sorted(mirror - ci_scripts)
    assert not missing, f"skill Local Mirror is missing: {missing}"
    assert not extra, f"skill Local Mirror runs scripts the job does not: {extra}"


def test_planted_ci_addition_registers_as_missing_from_the_skill() -> None:
    """Adding a job step without touching the skill must fail this suite
    (issue #914 AC2). This test proves the PARSER side: the planted step lands
    in the ci.yml script set and is absent from the skill mirror, which is
    exactly the condition ``test_mirror_matches_the_quality_contracts_job``
    asserts against — the enforcing assertion itself lives there."""
    ci_text = CI.read_text(encoding="utf-8")
    block = _quality_contracts_block(ci_text)
    planted = block.replace(
        "run: python scripts/check_n05_draftclaim_nullable.py",
        "run: python scripts/check_n05_draftclaim_nullable.py\n"
        "      - name: Planted contract check\n"
        "        run: python scripts/check_zzz_planted_parity_probe.py",
        1,
    )
    assert planted != block, "plant anchor not found in the quality-contracts block"
    ci_scripts = _ci_contract_scripts(planted)
    mirror = _skill_mirror_scripts(SKILL.read_text(encoding="utf-8"))
    assert "scripts/check_zzz_planted_parity_probe.py" in ci_scripts - mirror


def test_parsers_ignore_commented_script_mentions() -> None:
    """A commented script mention is not a step on either surface
    (plan-critic round 1, MAJOR-2). This proves the parser level: both the
    ci.yml block parser and the fence parser skip comment lines, so a
    commented mention cannot enter either script set."""
    ci_text = CI.read_text(encoding="utf-8")
    block = _quality_contracts_block(ci_text)
    planted = block.replace(
        "run: python scripts/check_n05_draftclaim_nullable.py",
        "run: python scripts/check_n05_draftclaim_nullable.py\n"
        "      # scripts/check_yyy_commented_probe.py (commented mention)",
        1,
    )
    ci_scripts = _ci_contract_scripts(planted)
    assert "scripts/check_yyy_commented_probe.py" not in ci_scripts

    skill_text = SKILL.read_text(encoding="utf-8")
    # Anchor the plant INSIDE the fenced Local Mirror block: the bare
    # "\npython scripts/run_bandit.py\n" line only exists in the fence (the
    # prose mentions are backtick-wrapped), and the assertion below proves
    # the plant really landed between the fence markers before the parser
    # runs (a plant outside the fence would make this control vacuous).
    anchor = "\npython scripts/run_bandit.py\n"
    assert skill_text.count(anchor) == 1, (
        "expected exactly one bare in-fence run_bandit.py line to anchor the plant"
    )
    planted_skill = skill_text.replace(
        anchor,
        "\npython scripts/run_bandit.py\n"
        "# scripts/check_yyy_commented_probe.py (commented mention)\n",
        1,
    )
    fence_start = planted_skill.index("```bash", planted_skill.index("## Local Mirror Commands"))
    plant_at = planted_skill.index("# scripts/check_yyy_commented_probe.py")
    assert fence_start < plant_at, "planted comment must land inside the fenced block"
    assert "scripts/check_yyy_commented_probe.py" not in _skill_mirror_scripts(
        planted_skill
    ), "a commented line inside the fenced block must not count as a command"


def test_mirror_section_states_its_scope_and_the_enforcing_test() -> None:
    """The section must name the parity authority and the enforcing test
    (issue #914 AC3) — the pre-fix section implied completeness it did not
    deliver."""
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("## Local Mirror Commands")
    end = text.index("```bash", start)
    section_intro = text[start:end]
    assert "quality-contracts" in section_intro, (
        "the Local Mirror section must name the ci.yml quality-contracts job "
        "as the parity authority"
    )
    assert PARITY_TEST_NAME in section_intro, (
        "the Local Mirror section must name the enforcing parity test"
    )


def test_prose_repository_contract_list_matches_the_job() -> None:
    """The prose "Repository contract job" list is the second mirrored surface
    (final-critic MINOR-1): it drifted with the fence before #914, so it gets
    the same bidirectional parity — every job script is described, and the
    prose names no script the job does not run."""
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("Repository contract job:")
    rest = text[start:]
    end = re.search(r"\n## ", rest[1:])
    section = rest[: end.end()] if end else rest
    prose = set(
        re.findall(r"python\s+(" + _SCRIPT + r")", section)
    )
    assert prose, "parser found no contract scripts in the prose list"
    ci_scripts = _ci_contract_scripts(CI.read_text(encoding="utf-8"))
    missing = sorted(ci_scripts - prose)
    extra = sorted(prose - ci_scripts)
    assert not missing, f"skill prose list is missing: {missing}"
    assert not extra, f"skill prose list names scripts the job does not run: {extra}"
