"""Committed coverage for the non-deprecated table-enumeration helper.

Issue #754 replaces ``AsyncConnection.table_names()`` with the paginated
``list_tables()`` response shape. The two branches that are unique to the new
helper — accumulating more than one page and refusing a repeated page token —
had no committed test until the PR #1170 feedback round (TF-002 / TI-001).
"""

from types import SimpleNamespace

import pytest

from app.services.vector_store import _list_table_names


class _PagedDB:
    """A connection stub with a fixed, finite sequence of list_tables pages.

    Calling it after the sequence is exhausted raises rather than looping, so
    a regression that removes either termination branch fails fast instead of
    hanging the suite.
    """

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    async def list_tables(self, page_token=None):
        self.calls.append(page_token)
        if not self._pages:
            raise AssertionError("list_tables called after all pages were consumed")
        names, next_token = self._pages.pop(0)
        return SimpleNamespace(tables=names, page_token=next_token)


async def test_list_table_names_accumulates_every_page_in_order():
    db = _PagedDB(
        [
            (["alpha"], "page-1"),
            (["beta"], "page-2"),
            (["gamma"], None),
        ]
    )

    assert await _list_table_names(db) == ["alpha", "beta", "gamma"]
    assert db.calls == [None, "page-1", "page-2"]


async def test_list_table_names_rejects_a_repeated_page_token():
    db = _PagedDB(
        [
            (["alpha"], "stuck"),
            (["beta"], "stuck"),
        ]
    )

    with pytest.raises(RuntimeError, match="repeated page token"):
        await _list_table_names(db)
    assert db.calls == [None, "stuck"]
