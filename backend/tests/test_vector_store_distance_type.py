"""Explicit ``distance_type`` on every dense vector query (issue #510 VECTOR-004).

Flat/brute-force LanceDB scans otherwise default to L2 even when
``vector_metric`` is cosine, so cosine-calibrated relevance thresholds discard
valid matches. Both dense-query closures (``search`` and
``_search_single_scale``) must call ``query.distance_type(settings.vector_metric)``
BEFORE ``to_list``.
"""

import pytest

from app.config import settings
from app.services.vector_store import VectorStore


class RecordingQuery:
    """LanceDB query-builder fake recording the call order on one query."""

    def __init__(self, table, row=None):
        self._table = table
        self._row = row if row is not None else {
            "id": "1_0", "file_id": "1", "text": "x", "_distance": 0.1
        }

    def distance_type(self, metric):
        self._table.events.append(("distance_type", metric))
        return self

    def bypass_vector_index(self):
        self._table.events.append(("bypass_vector_index",))
        return self

    def where(self, expr):
        self._table.events.append(("where", expr))
        return self

    def limit(self, n):
        self._table.events.append(("limit", n))
        return self

    async def to_list(self):
        self._table.events.append(("to_list",))
        return [dict(self._row)]


class RecordingTable:
    """LanceDB table fake: vector searches return a RecordingQuery; FTS raises
    so the hybrid arm never competes with the dense arm under observation."""

    def __init__(self, row=None):
        self.events = []
        self._row = row

    async def search(self, embedding, query_type="vector"):
        self.events.append(("search", query_type))
        if query_type != "vector":
            raise RuntimeError("FTS not used in this test")
        return RecordingQuery(self, row=self._row)

    async def count_rows(self, filter=None):
        # Below VECTOR_INDEX_MIN_ROWS → search() skips index creation entirely.
        return 5

    async def list_indices(self):
        return []


def _make_store(row=None):
    store = VectorStore()
    store.db = object()  # non-None → search() skips connect()
    store.table = RecordingTable(row=row)
    return store


def _assert_distance_type_before_to_list(events, expected_metric):
    dt_indexes = [i for i, e in enumerate(events) if e[0] == "distance_type"]
    tl_indexes = [i for i, e in enumerate(events) if e[0] == "to_list"]
    assert dt_indexes, f"distance_type was never called. Events: {events}"
    assert tl_indexes, f"to_list was never called. Events: {events}"
    assert dt_indexes[0] < tl_indexes[0], (
        f"distance_type must be applied before to_list. Events: {events}"
    )
    assert events[dt_indexes[0]][1] == expected_metric


class TestDistanceTypeOnDenseQueries:
    @pytest.mark.asyncio
    async def test_search_dense_calls_distance_type_cosine_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "vector_metric", "cosine")
        monkeypatch.setattr(settings, "multi_scale_indexing_enabled", False)
        store = _make_store()

        results = await store.search(
            [0.1, 0.2, 0.3], 10, query_text="", hybrid=False
        )

        assert len(results) == 1
        _assert_distance_type_before_to_list(store.table.events, "cosine")

    @pytest.mark.asyncio
    async def test_search_dense_records_l2_when_metric_is_l2(self, monkeypatch):
        monkeypatch.setattr(settings, "vector_metric", "l2")
        monkeypatch.setattr(settings, "multi_scale_indexing_enabled", False)
        store = _make_store()

        await store.search([0.1, 0.2, 0.3], 10, query_text="", hybrid=False)

        _assert_distance_type_before_to_list(store.table.events, "l2")

    @pytest.mark.asyncio
    async def test_search_single_scale_calls_distance_type_cosine(self, monkeypatch):
        monkeypatch.setattr(settings, "vector_metric", "cosine")
        store = _make_store()

        results = await store._search_single_scale(
            embedding=[0.1, 0.2, 0.3],
            scale="default",
            fetch_k=10,
            query_text="",  # no query text → dense-only path
            hybrid=True,
        )

        assert len(results) == 1
        _assert_distance_type_before_to_list(store.table.events, "cosine")

    @pytest.mark.asyncio
    async def test_search_single_scale_records_l2_when_metric_is_l2(self, monkeypatch):
        monkeypatch.setattr(settings, "vector_metric", "l2")
        store = _make_store()

        await store._search_single_scale(
            embedding=[0.1, 0.2, 0.3],
            scale="512",
            fetch_k=5,
            query_text="",
            hybrid=False,
        )

        _assert_distance_type_before_to_list(store.table.events, "l2")

    @pytest.mark.asyncio
    async def test_hybrid_search_dense_arm_also_sets_distance_type(self, monkeypatch):
        """With hybrid enabled but FTS failing, the dense arm still applies
        distance_type."""
        monkeypatch.setattr(settings, "vector_metric", "cosine")
        monkeypatch.setattr(settings, "rrf_legacy_mode", True)
        monkeypatch.setattr(settings, "hybrid_rrf_k", 60)
        monkeypatch.setattr(settings, "multi_scale_indexing_enabled", False)
        store = _make_store()

        results = await store.search(
            [0.1, 0.2, 0.3], 10, query_text="some query", hybrid=True
        )

        # FTS arm raised → dense-only results still returned.
        assert len(results) == 1
        _assert_distance_type_before_to_list(store.table.events, "cosine")
