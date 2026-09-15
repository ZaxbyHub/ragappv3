"""Issue #558 acceptance check AC3 (finding C13) — fallback preservation.

When the table exposes an older/foreign API shape such that ANY reasonable
query-builder attempt raises (async ``search()`` resolves to an object with
no ``.where``, sync ``query()`` raises), ``has_parent_window_text_sample``
must still return True via the 50-row ``head(50)`` row-scan fallback when
the sentinel sits inside that window. The fallback behavior is correct and
must survive any fix for the never-awaited-coroutine defect.

PRESERVING check: GREEN at base 2a7732a1 and must stay green after any
correct fix.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.vector_store import VectorStore


class _ForeignBuilder:
    """Result of a foreign API's ``search()``: no query-builder attributes."""


class _ForeignTable:
    """Older/foreign table shape: every primary-path entry point fails.

    - ``search()`` is async and resolves to an object with NO ``.where`` —
      a correctly-awaited chain still raises AttributeError.
    - ``query()`` (sync builder entry point) raises RuntimeError.
    - ``head(50)`` yields 10 dict rows whose metadata contains the
      ``parent_window_text`` sentinel — the fallback must find them.
    """

    async def search(self, *args, **kwargs) -> _ForeignBuilder:
        return _ForeignBuilder()

    def query(self, *args, **kwargs) -> "_ForeignTable":
        raise RuntimeError("foreign API shape: query() unavailable")

    async def head(self, n: int = 50, *args, **kwargs) -> list:
        return [
            {
                "metadata": json.dumps(
                    {"chunk_id": i, "parent_window_text": "wider context"}
                )
            }
            for i in range(10)
        ]


@pytest.mark.asyncio
async def test_fallback_head_scan_still_finds_sentinel_on_foreign_api_shape():
    """AC3: with every query-builder path raising, the head(50) row-scan
    fallback still returns True (GREEN at base and after any correct fix)."""
    table = _ForeignTable()
    # Bypass __init__: the method only reads ``self.table``.
    store = VectorStore.__new__(VectorStore)
    store.table = table

    result = await store.has_parent_window_text_sample()

    assert result is True
