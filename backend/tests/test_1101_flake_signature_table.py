"""Content pin for the Backend CI flake-signature table (issue #1101).

Issue #1101's deliverable is documentation: the ci-compatibility-audit skill
must carry the five documented Backend flake families — each row with its log
signature and its diff-foreign discriminator — plus the rerun decision rule,
with the classification protocol stated BEFORE that rule (AC1 + AC2, both
classed NON-EXECUTABLE(DOCS_ONLY)). This module is the executing substitute
evidence: it fails if the section is dropped, a family row loses its signature
or discriminator, a cross-referenced path rots, or the protocol ends up after
(or without) the rerun rule.

At the pre-fix base (324fbb19) the section does not exist, so every assertion
here fails there and passes at head.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / ".agents" / "skills" / "ci-compatibility-audit" / "SKILL.md"

SECTION = "## Backend CI flake signature table (issue #1101)"

# (family label, distinctive log-signature marker, discriminator marker)
FAMILIES = [
    (
        "xdist sqlite moving-set",
        "sqlite3.OperationalError: unable to open database file",
        "The failed-test SET differs between reruns",
    ),
    (
        "C32 embedding-reuse",
        "C32 CHECK: FAIL:",
        "reproduces on a master commit whose diff touches no application code",
    ),
    (
        "secretscan rglob-during-coverage-walk",
        "scripts/check_secretscan.py",
        "vanish-tolerant",
    ),
    (
        "whole-job freeze",
        "timeout-minutes: 60",
        "same silent cutoff at the same wall-clock on master",
    ),
    (
        "#1021 hang-class canary",
        "test_wedge_starved_pool_join_aborted_by_configured_timeout",
        "RUN_CI_HANG_CANARY",
    ),
]

# Every file a signature row names must stay real — a moved test or script
# would silently rot the classifier's cross-references.
REFERENCED_PATHS = [
    "backend/app/models/database.py",
    "backend/app/api/deps.py",
    "backend/tests/issue513_checks/test_c32_embedding_reuse_enrichment_retry.py",
    "backend/tests/test_ci_hang_canary.py",
    "scripts/check_secretscan.py",
]

PROTOCOL_HEADING = "### Classification protocol"
SET_CHECK = "Compare the failed-test SET across reruns"
SURFACE_CHECK = "Confirm the diff touches none of the failing surface."
# The canary row also says "gh run rerun --failed" as incident history, so the
# ordering assertion anchors on the decision-rule sentence, which exists only
# in the protocol block.
RERUN_RULE = "Only then run `gh run rerun <run-id> --failed`"


def _section() -> str:
    text = SKILL.read_text(encoding="utf-8")
    assert SECTION in text, f"flake-signature section missing from {SKILL}"
    return text[text.index(SECTION) :]


def test_section_sits_between_environment_caveats_and_output() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "## Environment caveats" in text and "## Output" in text
    caveats = text.index("## Environment caveats")
    section = text.index(SECTION)
    output = text.index("## Output")
    assert caveats < section < output, "table must stay between caveats and Output"


def test_all_five_families_carry_signature_and_discriminator() -> None:
    section = _section()
    for label, signature, discriminator in FAMILIES:
        assert label in section, f"family row missing from table: {label}"
        assert signature in section, f"log signature missing for family: {label}"
        assert discriminator in section, (
            f"diff-foreign discriminator missing for family: {label}"
        )


def test_referenced_signature_paths_exist() -> None:
    for rel in REFERENCED_PATHS:
        assert (REPO / rel).is_file(), f"table cross-reference rotted: {rel}"


def test_protocol_precedes_rerun_rule() -> None:
    section = _section()
    for marker in (
        PROTOCOL_HEADING,
        SET_CHECK,
        SURFACE_CHECK,
        RERUN_RULE,
    ):
        assert marker in section, f"classification protocol lost: {marker}"
    protocol = section.index(PROTOCOL_HEADING)
    set_check = section.index(SET_CHECK)
    surface_check = section.index(SURFACE_CHECK)
    rerun = section.index(RERUN_RULE)
    assert set_check > protocol and surface_check > protocol, (
        "both discriminator checks must live inside the protocol block"
    )
    assert rerun > surface_check, (
        "rerun decision rule must come after both discriminator checks — "
        "a red job is never reclassified as diff-foreign without them"
    )
