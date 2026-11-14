"""Arm's-length acceptance checks for the #920 doc-truth half of the collated
#901 + #920 + #927 trace (AC7, AC8, AC9, AC14).

Doc-content assertions against the three prose surfaces that still restate
the pre-#790 exclusivity story: the SPEC 4.1 module-responsibility row for
``draft_store.py`` ("All SQLite CRUD"), the ``draft_job_processor.py``
module docstring (claim-mediation phrased so a reader infers full store
mediation), and SPEC 10.3's unscoped status-write invariant. PR #921
scoped the store's own docstring; these checks hold the remaining surfaces
to the same owner map — and hold SPEC 10.3 and the store docstring to
agreement.

Every test here asserts the DESIRED behavior: the file is RED at base
b369c02f for the right reason and GREEN after the fix. Plain file reads
plus asserts only — no app import, no fixtures.
"""

import ast
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SPEC_PATH = _REPO_ROOT / "specs" / "draft-room" / "SPEC.md"
_PROCESSOR_PATH = (
    _REPO_ROOT / "backend" / "app" / "services" / "draft_job_processor.py"
)
_STORE_PATH = _REPO_ROOT / "backend" / "app" / "services" / "draft_store.py"

_CO_OWNER_BASENAMES = (
    "draft_room.py",
    "draft_job_processor.py",
    "draft_pipeline.py",
)
# The co-owner mechanism vocabulary the two status-contract surfaces
# (SPEC 10.3 and the draft_store.py module docstring) must share.
_MECHANISM_TERMS = ("owner map", "co-owner", "allowlist")


def _normalized(text):
    """Collapse all whitespace runs to single spaces (no flaky greps)."""
    return " ".join(text.split())


def _module_docstring(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree)
    assert docstring is not None, f"{path.name} lost its module docstring"
    return docstring


def _spec_section(number):
    """The text of one ``### <number> ...`` SPEC section, heading included."""
    lines = _SPEC_PATH.read_text(encoding="utf-8").splitlines()
    prefix = f"### {number} "
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(prefix)), None
    )
    assert start is not None, f"SPEC heading {prefix!r} not found"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("### ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


class TestN06ADocOwnerMapTruth(unittest.TestCase):
    """Desired-behavior doc checks — RED at base, GREEN after the fix."""

    # -- AC7 (#920.1): SPEC 4.1 row names the co-owners ---------------------

    def test_spec_41_row_names_the_declared_co_owners(self):
        section = _spec_section("4.1")
        rows = [
            _normalized(line)
            for line in section.splitlines()
            if line.startswith("| `backend/app/services/draft_store.py`")
        ]
        assert len(rows) == 1, (
            "SPEC 4.1 must carry exactly one draft_store.py responsibility "
            f"row; found {len(rows)}"
        )
        row = rows[0]
        missing = [name for name in _CO_OWNER_BASENAMES if name not in row]
        links_owner_map = "owner map" in row.lower() or "owner-map" in row.lower()
        assert links_owner_map or not missing, (
            "SPEC 4.1's module-responsibility row for draft_store.py must "
            "name the three declared co-owner modules "
            "(api/routes/draft_room.py, services/draft_job_processor.py, "
            "services/draft_pipeline.py) or link the owner map; the row "
            f"names none of them (missing {missing}): {row!r}"
        )
        assert "All SQLite CRUD" not in row, (
            "SPEC 4.1's draft_store.py row must drop the pre-owner-map "
            f"'All SQLite CRUD' exclusivity claim; row: {row!r}"
        )

    # -- AC8 (#920.2): processor docstring declares its direct writes --------

    def test_processor_docstring_declares_worker_direct_writes(self):
        docstring = _module_docstring(_PROCESSOR_PATH)
        missing = [
            target
            for target in (
                "drafts.status",
                "draft_jobs",
                "draft_inputs.parse_status",
            )
            if target not in docstring
        ]
        assert not missing, (
            "draft_job_processor.py's module docstring must declare the "
            "worker's direct writes (drafts.status fail/requeue settles, "
            "draft_jobs retry rows, draft_inputs.parse_status) as a declared "
            "co-owner of the Draft Room tables; the docstring does not "
            "mention: " + ", ".join(missing)
        )
        lowered = docstring.lower()
        assert "co-owner" in lowered or "co owner" in lowered, (
            "the declaration must name the worker a declared co-owner "
            "(the #790 owner-map story), not an unspecified bystander"
        )
        # The claim-mediation law stays scoped to claims (preserving half):
        # adding the co-owner declaration must not rewrite or drop it.
        assert "claim" in lowered, (
            "the claim-mediation law (atomic claims through DraftStore) "
            "must stay scoped to claims in the docstring"
        )

    # -- AC9 (#920.3): SPEC 10.3 invariant scoped to the owner map -----------

    def test_spec_103_invariant_scoped_to_the_owner_map(self):
        section = _normalized(_spec_section("10.3"))
        lowered = section.lower()
        scoped = any(term in lowered for term in _MECHANISM_TERMS)
        assert scoped, (
            "SPEC 10.3's status-write invariant must be scoped to the real "
            "owner map — acknowledging DraftStore transitions OR registered "
            "mirrored allowlists in the three declared co-owners "
            "(api/routes/draft_room.py, services/draft_job_processor.py, "
            "services/draft_pipeline.py) — instead of the unscoped "
            "pre-#790 claim; the section carries none of the scoping "
            f"vocabulary {list(_MECHANISM_TERMS)}: {section[:300]!r}"
        )
        assert (
            "No code may write an arbitrary status string. Centralize the "
            "following transitions and reject all others:" not in section
        ), (
            "SPEC 10.3 still opens with the unscoped formulation ('No code "
            "may write ... Centralize ... reject all others'); the invariant "
            "must be reworded to the owner-map scope"
        )

    # -- AC14 (#927.5): SPEC 10.3 and the store docstring agree --------------

    def test_spec_103_docstring_agreement_between_spec_and_store(self):
        spec_section = _normalized(_spec_section("10.3")).lower()
        store_docstring = _normalized(_module_docstring(_STORE_PATH)).lower()
        docstring_terms = {t for t in _MECHANISM_TERMS if t in store_docstring}
        spec_terms = {t for t in _MECHANISM_TERMS if t in spec_section}
        assert docstring_terms, (
            "the draft_store.py module docstring lost its #921 owner-map "
            "scoping; expected at least one of "
            f"{list(_MECHANISM_TERMS)} in the docstring"
        )
        assert spec_terms & docstring_terms, (
            "SPEC 10.3 and the draft_store.py module docstring must agree "
            "on who may write a status and through what: the docstring "
            f"declares the co-owner mechanism via {sorted(docstring_terms)} "
            f"while SPEC 10.3 declares {sorted(spec_terms) or 'none of them'} "
            "— the SPEC invariant is still the unscoped pre-#790 story the "
            "docstring already moved past"
        )


if __name__ == "__main__":
    unittest.main()
