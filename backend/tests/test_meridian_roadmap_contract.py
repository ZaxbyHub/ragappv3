"""F3 roadmap-contract guardrail (issue #229 recurrence sweep).

Defect class: roadmap-slot obligations left undischarged after their gating
dependencies close, because no follow-through slot executed the final
integration step. Predicate 4 of the #229 sweep (evidence-matrix obligations
without a corresponding docs/eval artifact), plus the build-identity coupling
the matrix claims: the qualification data must exist, must name one exact
build consistently, and must link the release note and performance baseline.
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MATRIX = REPO / "docs" / "eval" / "2026-09-meridian-qualification.md"
DATA = REPO / "docs" / "eval" / "2026-09-meridian-qualification-data.json"
RELEASE_NOTE = REPO / "docs" / "releases" / "pending" / "229-f3-meridian-qualification.md"
PERF_BASELINE = REPO / "docs" / "eval" / "2026-09-performance.md"

HEX40 = re.compile(r"^[0-9a-f]{40}$")


def test_f3_qualification_matrix_exists():
    assert MATRIX.is_file(), (
        "F3 roadmap obligation undischarged: docs/eval/2026-09-meridian-qualification.md "
        "(the exact-build evidence matrix required by issue #229) is missing"
    )


def test_f3_matrix_names_one_exact_build_matching_the_data():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    build = data["meta"]["build"]
    deployed = data["meta"]["deployed_revision"]
    assert HEX40.match(build), "meta.build must be a 40-hex sha, got %r" % build
    assert build == deployed, "meta.build must equal meta.deployed_revision"
    matrix_text = MATRIX.read_text(encoding="utf-8")
    assert "- Build: `%s`" % build in matrix_text, (
        "matrix Build Identity line must carry the same 40-hex build as the data JSON"
    )


def test_f3_matrix_links_release_note_and_performance_baseline():
    matrix_text = MATRIX.read_text(encoding="utf-8")
    assert "docs/releases/pending/229-f3-meridian-qualification.md" in matrix_text
    assert "docs/eval/2026-09-performance.md" in matrix_text
    assert RELEASE_NOTE.is_file(), "F3 pending release note missing"
    assert PERF_BASELINE.is_file(), "F2 performance baseline referenced by F3 is missing"


def test_f3_performance_profile_has_comparable_rows():
    matrix_text = MATRIX.read_text(encoding="utf-8")
    section = matrix_text.split("## Performance Profile", 1)[1]
    # Bound at the next heading: later tables (CI Gate Results) must not
    # satisfy this check after the performance rows are deleted (PRR-002).
    section = section.split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("| ") and "---" not in ln]
    rows = rows[1:]  # drop header row
    assert len(rows) >= 2, "performance profile must record at least two measured rows"
