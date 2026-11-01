"""Issue #715 acceptance check — audit finding T1-32 (FTS attribution).

``test_concurrent_requests_do_not_steal_fts_counts``

Contract under test: ``fts_exceptions`` reported in one request's retrieval
tuple must count ONLY that request's own FTS leg failures. The counter read
must be attributed per query, not read-and-zeroed from a single shared
instance counter — otherwise a concurrent request silently "steals" another
request's failure count and both requests report wrong hybrid-health
telemetry (the victim reports 0 exceptions it actually caused; the thief
reports exceptions it never caused).

Root cause verified at HEAD (665bbd09): ``VectorStore.get_fts_exceptions``
(vector_store.py L844-849) reads-and-zeros ONE instance-wide
``_fts_exceptions`` counter, and ``_execute_retrieval`` reads it once per
query (rag_engine.py ~L3294). With two overlapping requests, whichever
``_execute_retrieval`` reads first consumes the other's increment.

This check asserts the DESIRED post-fix behavior and is RED at HEAD with::

    AssertionError: assert 1 == 0   (request B steals request A's increment)

Design (deterministic — sequenced by asyncio events, no sleep races):

* Request A's dense leg BLOCKS on an ``asyncio.Event`` the test controls;
  its FTS leg fails exactly once (incrementing the store counter through
  the production code path: the fake table's FTS search raises, production
  ``_run_fts`` catches it and increments).
* Only after A's FTS failure is observed (via a second event set by the
  fake table immediately before the raise — the production increment in
  the same exception-dispatch step is complete before any other task
  resumes) does request B run TO COMPLETION and read its
  ``fts_exceptions`` (tuple index 6 of ``_execute_retrieval``'s return).
* Then A's dense leg is released and A reads its own count.

Only the Lance table is faked; the engine and the ``VectorStore`` (search,
FTS failure accounting, ``get_fts_exceptions``) are the real production
objects, so the check does not depend on the fix's internal shape.

Harness reuse: the ``_FakeLanceSearchQuery`` shape and the real-store +
``_make_engine`` wiring mirror
``tests/test_rag_engine_hybrid_status.py::test_rag_query_single_scale_hybrid_does_not_fallback_on_rrf``
(both are imported from that module).
"""

import asyncio
import os
import sys
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse the engine/table-query harness from the hybrid-status tests.
from test_rag_engine_hybrid_status import (  # noqa: E402
    _FakeLanceSearchQuery,
    _make_engine,
)

from app.services.vector_store import VectorStore  # noqa: E402

_QUERY_A = "request A question text"
_QUERY_B = "request B question text"
# Marker embeddings let the fake table tell the two requests' dense legs
# apart (the FTS leg is distinguished by its query_text).
_EMB_A = [1.0, 0.0]
_EMB_B = [0.0, 1.0]


class _GatedLanceSearchQuery:
    """Dense-arm query whose to_list() blocks until the test releases it."""

    def __init__(self, rows: List[dict], gate: asyncio.Event):
        self._rows = rows
        self._gate = gate

    def where(self, filter_expr):
        return self

    def limit(self, limit):
        return self

    async def to_list(self):
        await self._gate.wait()
        return list(self._rows)


class _AttributionFakeLanceTable:
    """Fake Lance table sequencing the two requests' search arms.

    * vector arm, request A (marker embedding) → dense results gated on
      ``dense_gate`` (request A cannot finish retrieval until released).
    * vector arm, request B → plain dense rows.
    * fts arm, request A → sets ``a_fts_raised`` then raises ONCE, so
      production ``_run_fts`` records the failure (counter += 1).
    * fts arm, request B → plain FTS rows.
    """

    def __init__(
        self,
        rows_a: List[dict],
        rows_b_dense: List[dict],
        rows_b_fts: List[dict],
        dense_gate: asyncio.Event,
        a_fts_raised: asyncio.Event,
    ):
        self._rows_a = rows_a
        self._rows_b_dense = rows_b_dense
        self._rows_b_fts = rows_b_fts
        self._dense_gate = dense_gate
        self._a_fts_raised = a_fts_raised

    async def search(self, query, query_type):
        if query_type == "vector":
            if float(query[0]) == 1.0:  # request A's marker embedding
                return _GatedLanceSearchQuery(self._rows_a, self._dense_gate)
            return _FakeLanceSearchQuery(self._rows_b_dense)
        if query_type == "fts":
            if query == _QUERY_A:
                # Signal first: the production increment in _run_fts's except
                # block runs in the same event-loop step as this raise, so
                # any task observing the event observes the increment too.
                self._a_fts_raised.set()
                raise RuntimeError("simulated FTS failure for request A")
            return _FakeLanceSearchQuery(self._rows_b_fts)
        raise AssertionError(f"unexpected query_type: {query_type}")

    async def list_indices(self):
        return []

    async def count_rows(self):
        return 0


async def test_concurrent_requests_do_not_steal_fts_counts(monkeypatch):
    """Request B's fts_exceptions must be 0 when only request A's FTS leg
    failed — the shared read-and-zero counter lets B steal A's increment.
    T1-32 FTS-attribution acceptance check (issue #715)."""
    dense_gate = asyncio.Event()
    a_fts_raised = asyncio.Event()

    rows_a = [
        {"id": "a-dense", "text": "request A dense row", "file_id": "fa",
         "metadata": {}, "_distance": 0.1},
    ]
    rows_b_dense = [
        {"id": "b-dense", "text": "request B dense row", "file_id": "fb",
         "metadata": {}, "_distance": 0.2},
    ]
    rows_b_fts = [
        {"id": "b-fts", "text": "request B fts row", "file_id": "fb",
         "metadata": {}, "_distance": 0.3},
    ]

    # Real VectorStore; only the Lance table is fake (same wiring as the
    # hybrid-status tests).
    store = VectorStore()
    store.db = MagicMock()
    store.table = _AttributionFakeLanceTable(
        rows_a, rows_b_dense, rows_b_fts, dense_gate, a_fts_raised
    )
    monkeypatch.setattr(store, "_maybe_create_vector_index", AsyncMock())
    # Multi-scale must stay off so search() uses the single-scale hybrid path
    # (dense + FTS arms), and the packing/evaluation extras must not interfere.
    monkeypatch.setattr(
        "app.services.vector_store.settings.multi_scale_indexing_enabled",
        False,
    )
    monkeypatch.setattr(
        "app.services.vector_store.settings.hybrid_rrf_k",
        60,
    )
    monkeypatch.setattr(
        "app.services.vector_store.settings.rrf_legacy_mode",
        False,
    )
    monkeypatch.setattr(
        "app.services.rag_engine.settings.context_max_tokens",
        0,
    )
    monkeypatch.setattr(
        "app.services.rag_engine.settings.retrieval_evaluation_enabled",
        False,
    )

    engine = _make_engine([], hybrid_search_enabled=True)
    engine.vector_store = store

    # Request A starts first: its FTS leg fails (counter += 1) while its
    # dense leg parks on dense_gate, so A's _execute_retrieval CANNOT reach
    # its get_fts_exceptions() read yet.
    task_a = asyncio.create_task(
        engine._execute_retrieval(
            query_embeddings=[("original", list(_EMB_A))],
            user_input=_QUERY_A,
            vault_id=None,
        )
    )
    await asyncio.wait_for(a_fts_raised.wait(), timeout=10)
    # Let A's task finish the exception-dispatch step (belt and braces; the
    # set→raise→increment stretch contains no awaits).
    await asyncio.sleep(0)

    # Request B runs to completion and reads the fts counter. It must see
    # ONLY its own FTS outcome (zero failures) — not A's pending increment.
    result_b = await engine._execute_retrieval(
        query_embeddings=[("original", list(_EMB_B))],
        user_input=_QUERY_B,
        vault_id=None,
    )

    # Release request A's dense leg; A now reads its own count.
    dense_gate.set()
    result_a = await task_a

    assert result_b[6] == 0, (
        "request B stole request A's FTS exception increment: B reported "
        f"fts_exceptions={result_b[6]} although only A's FTS leg failed"
    )
    assert result_a[6] == 1, (
        "request A lost its own FTS exception increment to the concurrent "
        f"read: A reported fts_exceptions={result_a[6]} although its FTS "
        "leg failed exactly once"
    )
