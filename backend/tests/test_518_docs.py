"""Issue #518 acceptance check C6 / AC6 — operator docs + pending release
note (executable documentation check).

NEW-SURFACE spec (frozen): the operator documentation lives in
``docs/operations.md`` (new file) and must contain, verbatim as headings:

* ``## Worker topology``       — scale-out worker topology
* ``## Lease and admission backend``
* ``## Outage behavior``
* ``## Operator recovery actions``
* ``## Scheduled backups``     — scheduled backup operation under the
  existing deployment support (docker-compose; no new scheduler service)

Hardware inventory sentence must name the deployed host and measured roles:
host ``172.16.50.159`` with ``2x RTX 2000E Ada 16 GB`` + ``1x RTX 1000``
(8 GB), TEI 1.9.3 serving ``harrier-oss-v1-0.6b`` on :8080 and
``bge-reranker-v2-m3`` on :8081, described as MEASURED roles
("measured role" wording present), with no unmeasured numeric
concurrent-users capacity claim.

Also required: a pending release note ``docs/releases/pending/518-*.md``
with a ``## What changed`` heading.

Base tree fails: docs/operations.md does not exist.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "operations.md"

REQUIRED_HEADINGS = [
    "## Worker topology",
    "## Lease and admission backend",
    "## Outage behavior",
    "## Operator recovery actions",
    "## Scheduled backups",
]

REQUIRED_TOKENS = [
    "172.16.50.159",
    "RTX 2000E Ada",
    "16 GB",
    "RTX 1000",
    "TEI 1.9.3",
    "harrier-oss-v1-0.6b",
    "8080",
    "bge-reranker-v2-m3",
    "8081",
]

# Numeric "N concurrent users" claims are unmeasured capacity statements.
UNMEASURED_CAPACITY_CLAIM = re.compile(r"\d+\s+concurrent\s+users", re.IGNORECASE)


def test_operations_doc_has_required_operator_sections():
    if not DOC_PATH.exists():
        raise AssertionError(
            "518-C6 OPERATIONS DOC MISSING: docs/operations.md does not "
            "exist (operator docs: worker topology, lease/admission "
            "backend, outage behavior, recovery actions, scheduled backups)"
        )
    text = DOC_PATH.read_text(encoding="utf-8")
    for heading in REQUIRED_HEADINGS:
        assert heading in text, (
            f"518-C6 OPERATIONS DOC MISSING SECTION: {heading!r} not found "
            "in docs/operations.md"
        )


def test_operations_doc_has_measured_hardware_inventory():
    assert DOC_PATH.exists(), (
        "518-C6 OPERATIONS DOC MISSING: docs/operations.md does not exist"
    )
    text = DOC_PATH.read_text(encoding="utf-8")
    missing = [tok for tok in REQUIRED_TOKENS if tok not in text]
    assert not missing, (
        "518-C6 HARDWARE INVENTORY INCOMPLETE: docs/operations.md is "
        f"missing required inventory tokens {missing}"
    )
    assert "2x" in text.lower() or "2×" in text, (
        "518-C6 HARDWARE INVENTORY INCOMPLETE: the deployed host's 2x "
        "RTX 2000E Ada cards are not named"
    )
    assert "measured role" in text.lower(), (
        "518-C6 MEASURED-ROLES WORDING MISSING: docs/operations.md must "
        "describe deployment roles as measured, not assumed"
    )
    claim = UNMEASURED_CAPACITY_CLAIM.search(text)
    assert claim is None, (
        "518-C6 UNMEASURED CAPACITY CLAIM: docs/operations.md contains a "
        f"numeric capacity claim ({claim.group(0)!r}) without measurement"
    )


def test_pending_release_note_exists_for_518():
    pending_dir = REPO_ROOT / "docs" / "releases" / "pending"
    notes = sorted(pending_dir.glob("518-*.md"))
    assert notes, (
        "518-C6 RELEASE NOTE MISSING: no docs/releases/pending/518-*.md "
        "pending release note exists"
    )
    for note in notes:
        content = note.read_text(encoding="utf-8")
        assert "## What changed" in content, (
            f"518-C6 RELEASE NOTE MALFORMED: {note.name} lacks a "
            "'## What changed' heading"
        )
