"""
Tests for FTS index validation at startup in lifespan.py.

All tests drive the REAL production function
``app.lifespan.validate_fts_index`` (extracted from the former inline
startup block) with controlled table doubles — no inline copies of the
validation logic:

1. FTS index missing + hybrid enabled → ERROR log emitted, returns False
2. FTS index exists + hybrid enabled → no ERROR log, returns True
3. hybrid disabled → the lifespan CALL SITE must not invoke the check at
   all (asserted structurally against the real ``lifespan()`` source: the
   single ``validate_fts_index`` call is nested inside the
   ``settings.hybrid_search_enabled`` guard, so a disabled flag means
   ``list_indices`` is never reached)
4. list_indices raises → ERROR logged, no exception propagated, returns False
5. empty indices list (boundary) → ERROR log emitted, returns False
6. list_indices returns None → TypeError caught by the production except
   clause → "Failed to check" ERROR logged, returns False
"""

import ast
import inspect
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.lifespan import lifespan, validate_fts_index

_LIFESPAN_LOGGER = "app.lifespan"


class MockIndex:
    """Minimal double for a LanceDB index object with a .name attribute."""

    def __init__(self, name: str):
        self.name = name


def _mock_table(indices=None, side_effect=None):
    """Table double whose list_indices returns ``indices`` or raises."""
    table = MagicMock()
    if side_effect is not None:
        table.list_indices = AsyncMock(side_effect=side_effect)
    else:
        table.list_indices = AsyncMock(return_value=indices)
    return table


class _CapturingHandler(logging.Handler):
    """Collect log records without pytest's caplog (unittest-native)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestFTSValidationAtStartup(unittest.IsolatedAsyncioTestCase):
    """Drive the REAL validate_fts_index with controlled table doubles."""

    # ── Test 1: FTS missing + hybrid enabled → ERROR logged ─────────────────────

    async def test_fts_missing_hybrid_enabled_logs_error(self):
        """No 'fts_text' index → ERROR describing the problem + False."""
        table = _mock_table(
            indices=[MockIndex("other_idx"), MockIndex("embedding_idx")]
        )
        with self.assertLogs(_LIFESPAN_LOGGER, level="ERROR") as captured:
            result = await validate_fts_index(table)
        self.assertIs(result, False)
        error_records = [
            r for r in captured.records if r.levelno >= logging.ERROR
        ]
        self.assertEqual(len(error_records), 1)
        self.assertIn("FTS index is missing", error_records[0].getMessage())
        self.assertIn("FTS", error_records[0].getMessage())

    # ── Test 2: FTS exists + hybrid enabled → no ERROR logged ──────────────────

    async def test_fts_exists_hybrid_enabled_no_error(self):
        """An existing 'fts_text' index → True, and no ERROR logged."""
        table = _mock_table(
            indices=[MockIndex("fts_text"), MockIndex("embedding_idx")]
        )
        handler = _CapturingHandler()
        logger = logging.getLogger(_LIFESPAN_LOGGER)
        logger.addHandler(handler)
        try:
            result = await validate_fts_index(table)
        finally:
            logger.removeHandler(handler)
        self.assertIs(result, True)
        error_records = [
            r for r in handler.records if r.levelno >= logging.ERROR
        ]
        self.assertEqual(error_records, [])
        table.list_indices.assert_awaited_once()

    # ── Test 3: hybrid disabled → no FTS check performed ────────────────────────

    def test_hybrid_disabled_skips_fts_check(self):
        """The hybrid gate lives at the lifespan CALL SITE (the extracted
        function validates unconditionally). Assert structurally against the
        real ``lifespan()`` source that its single ``validate_fts_index``
        call is nested inside the ``settings.hybrid_search_enabled`` guard —
        with the flag disabled, ``list_indices`` is never reached."""
        source = inspect.getsource(lifespan)
        tree = ast.parse(source)

        def _fts_calls(node) -> list:
            return [
                n
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "validate_fts_index"
            ]

        all_calls = _fts_calls(tree)
        self.assertEqual(
            len(all_calls),
            1,
            "lifespan() must call validate_fts_index exactly once",
        )

        gated_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (
                isinstance(test, ast.Attribute)
                and test.attr == "hybrid_search_enabled"
            ):
                gated_calls.extend(_fts_calls(node))
        self.assertEqual(
            gated_calls,
            all_calls,
            "the validate_fts_index call must be inside the "
            "settings.hybrid_search_enabled guard",
        )

    # ── Test 4: list_indices raises → ERROR logged, no exception propagated ─────

    async def test_list_indices_raises_logs_error_but_continues(self):
        """A failing list_indices → ERROR logged, False returned, nothing
        propagates (startup continues)."""
        table = _mock_table(side_effect=RuntimeError("list_indices failed"))
        with self.assertLogs(_LIFESPAN_LOGGER, level="ERROR") as captured:
            result = await validate_fts_index(table)
        self.assertIs(result, False)
        error_records = [
            r for r in captured.records if r.levelno >= logging.ERROR
        ]
        self.assertEqual(len(error_records), 1)
        self.assertIn(
            "Failed to check FTS index status", error_records[0].getMessage()
        )
        self.assertIn(
            "list_indices failed", error_records[0].getMessage()
        )

    # ── Boundary: empty list + hybrid enabled ─────────────────────────────────

    async def test_empty_indices_list_hybrid_enabled_logs_error(self):
        """An empty index list → same missing-FTS ERROR + False."""
        table = _mock_table(indices=[])
        with self.assertLogs(_LIFESPAN_LOGGER, level="ERROR") as captured:
            result = await validate_fts_index(table)
        self.assertIs(result, False)
        error_records = [
            r for r in captured.records if r.levelno >= logging.ERROR
        ]
        self.assertEqual(len(error_records), 1)
        self.assertIn("FTS index is missing", error_records[0].getMessage())

    # ── Boundary: list_indices returns None ───────────────────────────────────

    async def test_list_indices_returns_none_logs_error(self):
        """list_indices returning None → the production any() raises
        TypeError, caught by the except clause → "Failed to check" ERROR +
        False."""
        table = _mock_table(indices=None)
        with self.assertLogs(_LIFESPAN_LOGGER, level="ERROR") as captured:
            result = await validate_fts_index(table)
        self.assertIs(result, False)
        error_records = [
            r for r in captured.records if r.levelno >= logging.ERROR
        ]
        self.assertEqual(len(error_records), 1)
        self.assertIn(
            "Failed to check FTS index status", error_records[0].getMessage()
        )


if __name__ == "__main__":
    unittest.main()
