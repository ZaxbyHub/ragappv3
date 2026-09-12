"""Issue #258 (E2) acceptance checks — AC3 node (b) / TEST-003: lifespan FTS.

Phase 2.5 CHECKS ONLY (tier L). TEST-003's second half: the six startup-FTS
tests in ``test_lifespan_fts_validation.py`` inline-COPY the lifespan FTS
validation block; production ``app/lifespan.py`` (the
``if settings.hybrid_search_enabled:`` block inside ``lifespan()``) makes ZERO
calls that any test can observe — mutating the production block cannot fail
the suite (mutation-proof per the audit artifacts).

At base a543361 the validation logic is INLINE in ``lifespan()`` — there is no
importable production surface for a check to drive. Per the Phase 2.5 charter,
this node is therefore written against the TO-BE-EXTRACTED contract and is
NEW-SURFACE class: RED at base via ImportError, GREEN after Phase 4 extracts
the block.

Dependency note (explicit): this node REQUIRES the Phase-4 extraction before
it can pass. The contract it demands:

    ``from app.lifespan import validate_fts_index``

    ``async def validate_fts_index(table) -> bool``

      * ``table`` is the vector store's table object (anything exposing an
        async ``list_indices()`` whose results have a ``.name``).
      * Returns True iff an index named ``"fts_text"`` exists.
      * Returns False when the index is MISSING (and logs the error) — the
        missing-index condition must be observable by the caller, not just
        logged and swallowed.
      * Returns False (and logs) when ``list_indices()`` itself raises — it
        must never propagate.

The three fakes below pin each clause. Mutation probes (mutating the extracted
function to always return True / to raise) are Phase 4.5 territory.
"""

import unittest


class _Index:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeTable:
    """Table double with a controllable ``list_indices`` result."""

    def __init__(self, indices) -> None:
        self._indices = indices

    async def list_indices(self):
        return self._indices


class _RaisingTable:
    """Table double whose ``list_indices`` fails like a broken connection."""

    async def list_indices(self):
        raise RuntimeError("simulated lancedb failure")


class TestLifespanFTSValidationSurface(unittest.IsolatedAsyncioTestCase):
    """AC3 node (b): the extracted production FTS validation contract."""

    async def test_validate_fts_index_contract(self) -> None:
        # AC3 CHECK — app.lifespan exposes no validate_fts_index production
        # surface (the FTS validation block is still inline in lifespan()).
        print("AC3 CHECK: FAIL — no validate_fts_index production surface in app.lifespan (block still inline)")
        from app.lifespan import validate_fts_index

        # Index present -> True.
        present = await validate_fts_index(
            _FakeTable([_Index("other"), _Index("fts_text")])
        )
        # AC3 CHECK — present fts_text index not reported as valid.
        print("AC3 CHECK: FAIL — present fts_text index not reported as valid")
        self.assertIs(present, True)

        # Index missing -> False (observable, not swallowed into a log line).
        missing = await validate_fts_index(_FakeTable([_Index("other")]))
        # AC3 CHECK — missing fts_text index not reported to the caller.
        print("AC3 CHECK: FAIL — missing fts_text index not reported to the caller")
        self.assertIs(missing, False)

        # list_indices() raising -> False, never an exception.
        # AC3 CHECK — validate_fts_index propagated a list_indices failure.
        print("AC3 CHECK: FAIL — validate_fts_index propagated a list_indices failure")
        broken = await validate_fts_index(_RaisingTable())
        self.assertIs(broken, False)


if __name__ == "__main__":
    unittest.main()
