"""Issue #940: standing-contract pins for the memory FTS arm's post-#712
semantics, plus the one discriminating cap-ordering check.

AC1-AC3 are NEW-SURFACE pins: the official base leg for them is this file
not existing (pytest "file or directory not found" in the frozen replay);
run in-tree at the pre-fix tree they are GREEN because they pin the
semantics the shared #515 tokenizer already shipped (the decisions were
owed, not the behavior). AC4 is DISCRIMINATING: RED at base (the builder
caps RAW tokens at 64 inside build_fts_match_query before the memory call
site filters stop words, so 64 leading stop words decapitate every content
term), GREEN after the fix moves stop-word filtering ahead of every cap.
"""

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = types.ModuleType(_mod)

if "lancedb.index" not in sys.modules:
    _ldb_idx = types.ModuleType("lancedb.index")
    _ldb_idx.FTS = type("FTS", (), {})
    _ldb_idx.IvfPq = type("IvfPq", (), {})
    sys.modules["lancedb.index"] = _ldb_idx

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    for _sub in ("partition", "partition.auto", "chunking", "chunking.title",
                 "documents", "documents.elements"):
        sys.modules.setdefault(f"unstructured.{_sub}", types.ModuleType(f"unstructured.{_sub}"))
    sys.modules.setdefault("unstructured", types.ModuleType("unstructured"))

from app.models.database import SQLiteConnectionPool, init_db
from app.services.memory_store import _FTS_STOP_WORDS, MemoryStore


def _fresh_store(tmp_path):
    """Real MemoryStore under pytest's tmp_path so nothing leaks."""
    db_path = Path(str(tmp_path)) / "fts-semantics.db"
    init_db(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=2)
    return MemoryStore(pool=pool), pool


def test_prefix_match_semantics_is_deliberate(tmp_path):
    """AC1 (#940): every \\w+ token becomes a ``token*`` prefix term — the
    converged #515 semantics affirmed for the memory arm by #712/#940 as the
    STANDING contract. Rows carrying "cats" (but never a bare "cat") and
    "category" (same) must both be returned for the single-term query "cat";
    a row sharing no prefix must not match. A revert to exact-token matching
    fails this pin by name."""
    store, pool = _fresh_store(tmp_path)
    try:
        store.add_memory("the cats sleep on the sofa", category="facts")
        store.add_memory("category theory reading notes", category="facts")
        store.add_memory("unrelated budget spreadsheet numbers", category="facts")
        results = store.search_memories("cat", limit=5)
        contents = [r.content for r in results]
        assert any(c == "the cats sleep on the sofa" for c in contents), (
            "prefix matching is the standing semantics: 'cat' must match the "
            f"'cats'-only row; got {contents}"
        )
        assert any(c == "category theory reading notes" for c in contents), (
            "prefix matching is the standing semantics: 'cat' must match the "
            f"'category'-only row; got {contents}"
        )
        assert not any(c == "unrelated budget spreadsheet numbers" for c in contents), (
            f"a row sharing no token prefix must not match 'cat'; got {contents}"
        )
    finally:
        pool.close_all()


def test_eight_term_cap_is_deliberate(tmp_path):
    """AC2 (#940): the 8-content-term cap (memory_store's ``[:8]`` slice after
    stop-word removal) is a DELIBERATE standing limit, recorded here. A query
    with 10 content terms still matches a row carrying terms 1-8 only, and a
    row carrying ONLY term 9 is not findable by the 10-term query — so any
    future change to the cap fails loudly instead of silently shifting
    recall."""
    store, pool = _fresh_store(tmp_path)
    try:
        store.add_memory(
            "alpha bravo charlie delta echo foxtrot golf hotel summary",
            category="facts",
        )
        store.add_memory("india travel notes only", category="facts")
        query = (
            "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        )
        results = store.search_memories(query, limit=5)
        contents = [r.content for r in results]
        assert any(
            c == "alpha bravo charlie delta echo foxtrot golf hotel summary"
            for c in contents
        ), (
            "a row carrying content terms 1-8 must still match the 10-term "
            f"query under the 8-term cap; got {contents}"
        )
        assert not any(c == "india travel notes only" for c in contents), (
            "terms past the 8-content-term cap are dropped, so the "
            "term-9-only row must not be findable by the 10-term query; "
            f"got {contents}"
        )
    finally:
        pool.close_all()


def test_operator_words_literal_conjunctive_is_the_recorded_ac8_exception(
    tmp_path,
):
    """AC3 (#940): the formal exception record for issue #712's Constraints
    row ("The memory FTS leg's tokenization change must keep matching every
    ASCII query it already matches"). After the shared-tokenizer swap the
    uppercase operator words OR/AND/NOT/NEAR are literal, conjunctive prefix
    terms instead of accidental live FTS5 operators: "chief OR deputy"
    AND-matches chief + or + deputy, so ONLY the row carrying all three
    literal terms is returned and the chief-only row (which matched at base,
    where bare OR leaked into FTS5 as a live operator) is not. PR #931's
    body filed this narrowing under "Waivers: None"; this pin is the
    durable, searchable record that the narrowing is a decided exception to
    that constraint, not an unrecorded drift."""
    store, pool = _fresh_store(tmp_path)
    try:
        store.add_memory("Justice Sakyi is the AFOMIS Chief", category="facts")
        store.add_memory(
            "choose the chief or the deputy for review", category="facts"
        )
        results = store.search_memories("chief OR deputy", limit=5)
        contents = [r.content for r in results]
        assert contents == ["choose the chief or the deputy for review"], (
            "operator words are literal conjunctive prefix terms (the "
            "recorded exception to #712's ASCII-preservation constraint): "
            f"only the all-three-terms row may match; got {contents}"
        )
    finally:
        pool.close_all()


def test_stop_word_filter_precedes_raw_token_cap(tmp_path):
    """AC4 (#940), DISCRIMINATING: stop-word filtering must precede every
    token cap. RED at base: build_fts_match_query caps RAW tokens at
    max_tokens=64 before the memory call site filters stop words, so a query
    whose first 64 raw tokens are all stop words yields no content term and
    returns [] (probe-verified at the anchor: 64 leading stop words + a
    content term -> 0 rows, 63 -> 1 row). GREEN after the fix (filter-then-
    cap at the tokenizer or call site): the content-term row is returned no
    matter how many stop words lead the query."""
    store, pool = _fresh_store(tmp_path)
    try:
        store.add_memory("quarterly roadmap planning notes", category="task")
        stop_cycle = sorted(_FTS_STOP_WORDS)

        def _stop_words(count):
            return [stop_cycle[i % len(stop_cycle)] for i in range(count)]

        # Control (green at BOTH ends): at 63 leading stop words the content
        # term sits inside the raw cap, is filtered forward, and matches —
        # proving the row itself is findable and only the 64-stop-word
        # decapitation is under test.
        control_query = " ".join(_stop_words(63)) + " roadmap"
        control_results = store.search_memories(control_query, limit=5)
        assert len(control_results) == 1, (
            "control leg: 63 leading stop words must not hide the content "
            f"term; got {len(control_results)} rows "
            f"({[r.content for r in control_results]})"
        )

        oversized_query = " ".join(_stop_words(64)) + " roadmap"
        results = store.search_memories(oversized_query, limit=5)
        assert len(results) >= 1, (
            "a query whose first 64 raw tokens are all stop words must still "
            "return the content-term row: stop-word filtering has to precede "
            f"every cap; got {len(results)} rows for the oversized query"
        )
    finally:
        pool.close_all()
