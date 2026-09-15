"""Issue #558 acceptance check AC1 (finding C13) — stub-table variant.

``VectorStore.has_parent_window_text_sample`` must decide the parent-window
startup diagnostic with a real query-builder chain: ``.where``/``.limit``
invoked on an AWAITED builder object (never on a coroutine), with no
``RuntimeWarning: coroutine ... was never awaited`` emitted, and the True
answer must come from the primary query path — not the ``head(50)``
row-scan fallback (the stub's ``head(50)`` window deliberately contains no
sentinel row, so a fallback-decided answer is False).

DISCRIMINATING check: RED at base 2a7732a1. The method writes
``await self.table.search().where(...)`` — the ``await`` binds only to
``search()``, so ``.where``/``.limit`` run on the coroutine object,
AttributeError drops the call into the 50-row fallback (returns False), and
the abandoned coroutine is GC'd with a never-awaited RuntimeWarning.
"""
import gc
import os
import sys
import warnings

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.vector_store import VectorStore


class _RecordingBuilder:
    """Query-builder double: records .where/.limit calls and produces the
    sentinel row, so any awaited-builder chain (async ``search()`` awaited
    before chaining, or the sync ``query()`` builder) can succeed."""

    def __init__(self, calls: list) -> None:
        self._calls = calls

    def where(self, *args, **kwargs) -> "_RecordingBuilder":
        self._calls.append(("where", args, kwargs))
        return self

    def limit(self, *args, **kwargs) -> "_RecordingBuilder":
        self._calls.append(("limit", args, kwargs))
        return self

    async def to_list(self, *args, **kwargs) -> list:
        return [{"metadata": '{"parent_window_text": "wider context"}'}]


class _StubTable:
    """Table double exposing BOTH plausible primary-path entry points.

    ``search()`` is an async method returning the recording builder and
    ``query()`` is a sync method returning the same builder — the check is
    agnostic about which entry point a fix uses, as long as the chain runs
    on a builder, not a coroutine. ``head(50)`` is the fallback data source:
    50 rows WITHOUT the sentinel, so only the primary path can return True.
    """

    def __init__(self) -> None:
        self.builder_calls: list = []

    async def search(self, *args, **kwargs) -> _RecordingBuilder:
        return _RecordingBuilder(self.builder_calls)

    def query(self, *args, **kwargs) -> _RecordingBuilder:
        return _RecordingBuilder(self.builder_calls)

    async def head(self, n: int = 50, *args, **kwargs) -> list:
        return [{"metadata": '{"chunk_id": %d}' % i} for i in range(n)]


@pytest.mark.asyncio
async def test_stub_query_chain_runs_on_builder_without_never_awaited_warning():
    """AC1: the filter chain runs on an awaited builder; True comes from the
    primary query, with no swallowed RuntimeWarning (RED at base)."""
    table = _StubTable()
    # Bypass __init__ (no lancedb connection needed): the method only reads
    # ``self.table`` (see vector_store.py:612).
    store = VectorStore.__new__(VectorStore)
    store.table = table

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await store.has_parent_window_text_sample()
        # Sweep any deferred coroutine finalization into the capture window.
        gc.collect()

    # (a) The sentinel is found — only the primary query path returns True
    # here (the fallback window has no sentinel row).
    assert result is True
    # (b) No never-awaited coroutine was destroyed.
    never_awaited = [
        w
        for w in caught
        if issubclass(w.category, RuntimeWarning)
        and "never awaited" in str(w.message)
    ]
    assert not never_awaited, [
        str(w.message) for w in never_awaited
    ]
    # (c) .where/.limit were invoked on a builder object, not a coroutine.
    assert table.builder_calls, "no .where/.limit calls recorded on a builder"
    assert any(call[0] == "where" for call in table.builder_calls)
