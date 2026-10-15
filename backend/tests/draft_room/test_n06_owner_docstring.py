"""Owner-map docstring contract for issue #790 (Workstream N PR 6 of 6).

``draft_store.py`` claimed for its whole life that "no other module writes
these tables" while 26 direct write expressions existed in three other
production modules — 8 in ``api/routes/draft_room.py``, 7 in
``services/draft_job_processor.py`` and 11 in ``services/draft_pipeline.py``
(the ENH-13 review's enumeration, reproduced line-for-line by the AST census
in ``test_table_ownership_census.py``). This test pins the honest contract
the census enforces: the store's module docstring must not claim exclusive
ownership, must name every co-owner the census declares, and must name every
table the census governs — so the prose map and the enforced map cannot drift
apart silently.
"""

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import app.services.draft_store as draft_store  # noqa: E402

_CENSUS_PATH = Path(__file__).with_name("test_table_ownership_census.py")

#: Exclusivity phrasings that would reinstate the false contract. A curated
#: set, not a semantic classifier: broadening this to catch every paraphrase
#: would false-positive on the owner map itself, which legitimately says
#: "three declared co-owners also write these tables directly".
_EXCLUSIVITY_CLAIMS = (
    "no other module writes these tables",
    "owns *all* draft room crud",
    "is the only module that writes",
    "the only module that writes",
    "solely owns",
    "no other module performs draft room writes",
    # Added after the #921 review: the replacement wording must not reintroduce
    # the same exclusivity in a different sentence.
    "every read/write goes through this module",
    "all live here",
)


def _load_census() -> object:
    """Import the census module so the docstring is checked against the map
    the census actually enforces, not a hand-copied duplicate."""
    spec = importlib.util.spec_from_file_location("_n06_census", _CENSUS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_tables(doc: str) -> set[str]:
    """The table set the docstring's own declaration sentence enumerates.

    Scoped to that sentence on purpose: a bare ``table in doc`` check is
    satisfied by a co-owner bullet that happens to name the same table, so
    dropping a table from the six-table declaration would go unnoticed.
    """
    marker = "the six Draft Room tables ("
    start = doc.index(marker) + len(marker)
    end = doc.index(")", start)
    tokens = (t.strip().strip("`").strip() for t in doc[start:end].split(","))
    return {token for token in tokens if token}


def test_draft_store_docstring_declares_the_real_owner_map() -> None:
    doc = draft_store.__doc__ or ""
    # The historical sentence is asserted FIRST and as a plain boolean so the
    # frozen acceptance check's expect signature (``assert True is False``) is
    # what a regression at base produces; the broader phrase set follows and
    # only matters once that one is gone.
    claims_exclusive = "no other module writes these tables" in doc.lower()
    assert claims_exclusive is False, (
        "draft_store's module docstring still claims exclusive table "
        "ownership, but direct write expressions live in "
        "api/routes/draft_room.py, draft_job_processor.py and "
        "draft_pipeline.py; declare them as co-owners instead (issue #790)"
    )
    paraphrases = [phrase for phrase in _EXCLUSIVITY_CLAIMS if phrase in doc.lower()]
    assert not paraphrases, (
        f"draft_store's module docstring still claims exclusive table "
        f"ownership ({paraphrases}); the census enforces a multi-owner map "
        "and the docstring must not say otherwise (issue #790)"
    )

    census = _load_census()
    co_owners = sorted(m for m in census.OWNER_MODULES if m != "services/draft_store.py")
    for co_owner in co_owners:
        assert co_owner in doc, (
            f"draft_store's module docstring must name {co_owner} as a "
            "co-owner of the Draft Room tables: the census enforces that "
            "owner set, and a co-owner the docstring omits is a map that "
            "drifts the moment someone reads only one of the two (issue #790)"
        )
    declared = _declared_tables(doc)
    assert declared == set(census.CENSUS_TABLES), (
        f"draft_store's module docstring declares {sorted(declared)} as the six "
        f"tables it owns, but the census governs {sorted(census.CENSUS_TABLES)}; "
        "the prose map and the enforced map must be the same set (issue #790)"
    )


if __name__ == "__main__":
    raise SystemExit(
        "run this module with pytest: "
        "cd backend && python -m pytest tests/draft_room/test_n06_owner_docstring.py"
    )
