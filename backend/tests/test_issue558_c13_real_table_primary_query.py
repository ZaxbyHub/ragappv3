"""Issue #558 acceptance check AC2 (finding C13) — real lancedb table.

On a real lancedb 0.36.0 table whose ONLY ``parent_window_text`` row sits at
index 55 — BEYOND the 50-row ``head(50)`` fallback window — the primary
metadata query (not the fallback scan) must decide the answer:
``has_parent_window_text_sample()`` returns True and no
``RuntimeWarning: coroutine ... was never awaited`` is emitted.

DISCRIMINATING check: RED at base 2a7732a1. The base code chains
``.where``/``.limit`` on the raw ``search()`` coroutine (AttributeError) and
falls into the fallback, which scans only the first 50 rows (and, on
lancedb 0.36.0, ``head()`` returns a pyarrow Table whose iteration yields
columns, not dict rows) — so the sentinel at row 55 is invisible and the
method returns False.
"""
import gc
import json
import os
import sys
import warnings

import lancedb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.vector_store import VectorStore

ROW_COUNT = 60
SENTINEL_INDEX = 55  # beyond the 50-row fallback window


@pytest.mark.asyncio
async def test_real_table_primary_query_finds_sentinel_beyond_fallback_window(
    tmp_path,
):
    """AC2: sentinel at row 55 (outside head(50)) is found by the primary
    query with no never-awaited RuntimeWarning (RED at base)."""
    db = await lancedb.connect_async(str(tmp_path / "lancedb"))
    rows = []
    for i in range(ROW_COUNT):
        metadata = {"chunk_id": i}
        if i == SENTINEL_INDEX:
            metadata["parent_window_text"] = (
                "wider context beyond the fallback window"
            )
        rows.append({"vector": [float(i), 1.0], "metadata": json.dumps(metadata)})
    table = await db.create_table("chunks", rows)

    # Bypass __init__ (no settings-driven paths): the method only reads
    # ``self.table`` (see vector_store.py:612).
    store = VectorStore.__new__(VectorStore)
    store.table = table

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await store.has_parent_window_text_sample()
        # Sweep any deferred coroutine finalization into the capture window.
        gc.collect()

    assert result is True
    never_awaited = [
        w
        for w in caught
        if issubclass(w.category, RuntimeWarning)
        and "never awaited" in str(w.message)
    ]
    assert not never_awaited, [str(w.message) for w in never_awaited]
