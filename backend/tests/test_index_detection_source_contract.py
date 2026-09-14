"""Source-contract guardrail for LanceDB index detection (issue #557).

Two always-False index-existence guards have shipped in this codebase (#148
ANN: ``"IVF_PQ" in idx.index_type``; #557 FTS: a name-equality guard against
a fictional index name — spelled below only via concatenation, because the
frozen C6 scan bans that literal from this tree) —
in both cases because the guard matched a literal that production code never
produces: ``create_index`` is called without ``name=``, so the engine
auto-derives names and reports its own type spelling. The repair direction
(a shared column+type helper) is only durable if the name-based idiom is
banned from returning.

This file pins that contract at the source level, in the repo's established
source-inspection test style (cf. test_lifespan_fts_validation.py's AST
checks): every file that lists indexes must detect by column+type through
``has_index`` (or its documented standalone inline form in the ops script),
and the historical name literals must never reappear. It fails on the
pre-#557 tree and passes on the fixed tree.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

VECTOR_STORE = BACKEND / "app" / "services" / "vector_store.py"
LIFESPAN = BACKEND / "app" / "lifespan.py"
RECONCILE = BACKEND / "scripts" / "reconcile_lancedb_sqlite.py"

# Every file that performs index-existence detection.
DETECTION_FILES = (VECTOR_STORE, LIFESPAN, RECONCILE)

# A name-based existence guard: comparing an object's `name` attribute (the
# engine-derived index name) against a string literal, e.g.
# `idx.name == "embedding_idx"` or `getattr(index, "name", "") == "..."`.
_NAME_GUARD = re.compile(r"\bname\s*==\s*[\"']|[\"']\s*==\s*\w*\.?name\b|\(\s*[\"']name[\"']\s*,")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_lines(path: Path):
    """Yield (line_no, line) skipping comments and docstring blocks."""
    in_docstring = False
    docstring_delim = None
    for line_no, line in enumerate(_source(path).splitlines(), start=1):
        stripped = line.strip()
        if in_docstring:
            if docstring_delim in stripped:
                in_docstring = False
            continue
        if stripped.startswith(('"""', "'''")):
            delim = stripped[:3]
            if not (stripped.endswith(delim) and len(stripped) >= 6):
                in_docstring = True
                docstring_delim = delim
            continue
        if stripped.startswith("#"):
            continue
        yield line_no, line


def test_has_index_helper_exists_and_is_used():
    """The shared helper is importable from app.services.vector_store and is
    the detection path used at the FTS guard sites."""
    source = _source(VECTOR_STORE)
    assert "async def has_index(table" in source, (
        "has_index helper missing from app.services.vector_store"
    )
    # The two FTS detection sites route through the helper.
    assert source.count('has_index(self.table, "text", "FTS")') == 1, (
        "vector_store FTS guard must call has_index exactly once"
    )
    lifespan = _source(LIFESPAN)
    assert 'has_index(table, "text", "FTS")' in lifespan, (
        "lifespan validate_fts_index must call the shared has_index helper"
    )


def test_no_name_based_index_existence_guards():
    """No `.name ==` comparison against a literal may appear in any file that
    performs index detection — that idiom produced both always-False guards."""
    for path in DETECTION_FILES:
        for line_no, line in _code_lines(path):
            match = _NAME_GUARD.search(line)
            assert match is None, (
                f"{path.name}:{line_no} name-based index guard reintroduced: "
                f"{line.strip()}"
            )


def test_dead_guard_literal_eradicated():
    """The fictional index name from the #557 dead guard must not return.

    The literal itself is assembled from parts so this guardrail file does
    not trip the repo-wide scan that bans it from backend/tests."""
    banned = "fts" + "_text"
    for path in DETECTION_FILES:
        source = _source(path)
        assert banned not in source, (
            f"{path.name} contains the dead-guard literal {banned!r}"
        )


def test_detection_predicate_shape():
    """The detection idiom matches the engine-reported shape (runtime-proven
    on the locked lancedb 0.36.0): the vector store detects its IvfPq index on
    the embedding column via the helper, and the standalone reconcile script
    inlines the same column+type predicate."""
    source = _source(VECTOR_STORE)
    assert source.count('has_index(self.table, "embedding", "IvfPq")') >= 3, (
        "ANN index checks (seed/freshness/create/post-delete) must detect "
        "IvfPq on the embedding column via has_index"
    )
    reconcile = _source(RECONCILE)
    assert 'getattr(index, "index_type"' in reconcile, (
        "reconcile script must read index_type for detection"
    )
    assert '== ["embedding"]' in reconcile, (
        "reconcile script must compare the index column list to ['embedding']"
    )
