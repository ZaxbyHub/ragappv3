"""Live-append IvfPq retrain contracts (issue #1065).

Every live append through ``VectorStore.add_chunks`` increments
``_index_mutation_generation`` and then always calls
``_maybe_create_vector_index`` while still holding the write lock. The skip
predicate inside that method requires
``_last_index_build_generation == _index_mutation_generation``, which the
just-incremented generation makes unsatisfiable — so at >=256 rows with an
existing IvfPq index every append runs a full-corpus
``create_index(column="embedding", config=IvfPq(...), replace=True)`` retrain
under the write lock. A companion hazard: a fix that merely skips the create
but leaves the generation bookkeeping stale moves the same retrain onto the
next ``search()`` (the ``_vector_index_needs_creation`` probe takes the write
lock and calls ``_maybe_create_vector_index`` again).

These specs pin the contract at both ends: a live append onto an index whose
bookkeeping was already fresh neither retrains the index nor leaves the
bookkeeping stale for the next search. RED at base, GREEN after the fix.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.vector_store import VectorStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_table(row_count: int = 300, has_ivfpq: bool = False):
    """Return a fully-mocked LanceDB table object.

    Mirrors the established ``_make_mock_table`` pattern from
    backend/tests/test_ingestion_integrity.py: the index listing is a
    shape-complete fake whose ``columns`` / ``index_type`` attributes are what
    ``vector_store.has_index`` detects (issue #557).
    """
    mock_idx = MagicMock()
    mock_idx.name = "embedding_idx"
    mock_idx.columns = ["embedding"]
    mock_idx.index_type = "IvfPq"

    table = MagicMock()
    table.count_rows = AsyncMock(return_value=row_count)
    table.list_indices = AsyncMock(return_value=[mock_idx] if has_ivfpq else [])
    table.drop_index = AsyncMock(return_value=None)
    table.create_index = AsyncMock(return_value=None)
    table.add = AsyncMock(return_value=None)
    table.optimize = AsyncMock(return_value=None)
    table.delete = AsyncMock(return_value=None)
    return table


def _reveal_index_after_create(table) -> None:
    """Make create_index side-effect the mock's index listing, like LanceDB.

    A static ``list_indices`` mock keeps reporting the pre-create state, so
    post-create freshness probes would wrongly claim the just-built index
    does not exist. Re-point the listing at a shape-complete IvfPq fake once
    ``create_index`` is awaited, matching what a real table reports.
    """
    mock_idx = MagicMock()
    mock_idx.name = "embedding_idx"
    mock_idx.columns = ["embedding"]
    mock_idx.index_type = "IvfPq"

    async def _created(**kwargs):
        table.list_indices = AsyncMock(return_value=[mock_idx])
        return None

    table.create_index = AsyncMock(side_effect=_created)


def _record(
    chunk_index: int, file_id: str = "1", text: str = "hello world"
) -> dict:
    """Minimal live-append record with every key add_chunks requires."""
    return {
        "id": f"{file_id}_{chunk_index}",
        "text": text,
        "file_id": file_id,
        "chunk_index": chunk_index,
        "vault_id": "1",
        "chunk_scale": "default",
        "embedding": [0.1, 0.2, 0.3, 0.4],
    }


def _replace_true_calls(table) -> list:
    """Return the awaited create_index calls whose kwargs carry replace=True."""
    return [
        call
        for call in table.create_index.await_args_list
        if call.kwargs.get("replace") is True
    ]


def _fresh_store(table, row_count: int, tmp_path: Path) -> VectorStore:
    """Store wired to ``table`` with FRESH index bookkeeping.

    Fresh means the existing IvfPq index was built for exactly ``row_count``
    rows at the current mutation generation, so no index work is owed.
    """
    store = VectorStore(db_path=tmp_path / "lancedb")
    store.table = table
    store._embedding_dim = 4
    store._last_index_build_row_count = row_count
    store._last_index_build_generation = 0
    store._index_mutation_generation = 0
    return store


# ---------------------------------------------------------------------------
# Frozen contract node (issue #1065)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_live_add_does_not_replace_existing_ivfpq(tmp_path):
    """A live append onto a fresh IvfPq index must not retrain it.

    Drive a REAL ``VectorStore.add_chunks`` against a fully-mocked LanceDB
    table that (i) reports 300 rows (>= the 256 VECTOR_INDEX_MIN_ROWS
    threshold), (ii) already carries an IvfPq index on the embedding column,
    and (iii) had FRESH bookkeeping before the add
    (``_last_index_build_row_count == 300`` and
    ``_last_index_build_generation == _index_mutation_generation == 0``).
    After the add, the next-search freshness probe
    (``_vector_index_needs_creation``) reports the index fresh, and
    ``table.create_index`` was never awaited with ``replace=True`` — no
    full-corpus retrain under the write lock on the append path, and no
    stale-generation retrain pushed onto the next search either.
    RED at base (the just-incremented generation makes the skip predicate's
    third conjunct unsatisfiable, so the append retrains), GREEN after the
    fix.
    """
    table = _make_mock_table(row_count=300, has_ivfpq=True)
    store = _fresh_store(table, row_count=300, tmp_path=tmp_path)

    with patch("app.services.vector_store.settings") as mock_settings, patch(
        "app.services.vector_store.IvfPq"
    ) as mock_ivfpq:
        mock_settings.optimize_mode = "manual"
        mock_settings.embedding_dim = 32
        mock_settings.vector_metric = "cosine"
        mock_settings.write_lock_timeout_seconds = 30
        mock_ivfpq.return_value = MagicMock()
        store._get_expected_embedding_dim = AsyncMock(return_value=4)

        await store.add_chunks([_record(0)])

    # The append itself happened.
    table.add.assert_awaited_once()

    # The next search() must find the index fresh — bookkeeping stays
    # coherent with the mutation generation, so the search path neither
    # takes the write lock nor retries the create.
    needs_creation = await store._vector_index_needs_creation()
    assert needs_creation is False, (
        "next-search freshness probe must report the fresh IvfPq index "
        f"fresh after a live append (got needs_creation={needs_creation}); "
        "a stale generation bookkeeping pushes the same retrain onto search()"
    )

    # No full-corpus retrain under the write lock during the append.
    replace_calls = _replace_true_calls(table)
    assert not replace_calls, (
        "live append onto a fresh IvfPq index must not retrain it, but "
        f"table.create_index was awaited with replace=True "
        f"{len(replace_calls)} time(s): {replace_calls}"
    )


# ---------------------------------------------------------------------------
# Supporting legs (not frozen): threshold no-op, genuine first create, and
# the enrichment-style add_chunks_then_delete_ids swap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_add_below_256_rows_creates_no_index(tmp_path):
    """Below the VECTOR_INDEX_MIN_ROWS threshold an append never creates an index.

    Pins the threshold no-op so the fix cannot trade the retrain for eager
    creation on small tables. Holds at base and after the fix.
    """
    table = _make_mock_table(row_count=200, has_ivfpq=True)
    store = _fresh_store(table, row_count=200, tmp_path=tmp_path)

    with patch("app.services.vector_store.settings") as mock_settings, patch(
        "app.services.vector_store.IvfPq"
    ) as mock_ivfpq:
        mock_settings.optimize_mode = "manual"
        mock_settings.embedding_dim = 32
        mock_settings.vector_metric = "cosine"
        mock_settings.write_lock_timeout_seconds = 30
        mock_ivfpq.return_value = MagicMock()
        store._get_expected_embedding_dim = AsyncMock(return_value=4)

        await store.add_chunks([_record(0)])

    table.add.assert_awaited_once()
    table.create_index.assert_not_awaited()
    assert await store._vector_index_needs_creation() is False


@pytest.mark.asyncio
async def test_live_add_at_threshold_without_index_still_creates_ivfpq(tmp_path):
    """A genuine first create (no existing IvfPq index) still happens.

    At >=256 rows with NO IvfPq index on the embedding column, the append
    path builds one exactly once with replace=True. Pins that the fix only
    skips redundant retrains of an existing fresh index, never the first
    build. Holds at base and after the fix.
    """
    table = _make_mock_table(row_count=300, has_ivfpq=False)
    _reveal_index_after_create(table)
    store = _fresh_store(table, row_count=0, tmp_path=tmp_path)

    with patch("app.services.vector_store.settings") as mock_settings, patch(
        "app.services.vector_store.IvfPq"
    ) as mock_ivfpq:
        mock_settings.optimize_mode = "manual"
        mock_settings.embedding_dim = 32
        mock_settings.vector_metric = "cosine"
        mock_settings.write_lock_timeout_seconds = 30
        mock_ivfpq.return_value = MagicMock()
        store._get_expected_embedding_dim = AsyncMock(return_value=4)

        await store.add_chunks([_record(0)])

    table.create_index.assert_awaited_once()
    assert table.create_index.await_args.kwargs.get("replace") is True
    # Bookkeeping records the build so the next search is not charged for it.
    assert store._last_index_build_row_count == 300
    assert store._last_index_build_generation == store._index_mutation_generation
    assert await store._vector_index_needs_creation() is False


@pytest.mark.asyncio
async def test_add_chunks_then_delete_ids_swap_does_not_replace_fresh_index(
    tmp_path,
):
    """The enrichment swap (add enriched rows, delete old ids) must not retrain.

    Mirrors the second write of post-index enrichment
    (``DocumentProcessor._swap_in_enriched_vector_records`` ->
    ``VectorStore.add_chunks_then_delete_ids``, document_processor.py
    ~2419-2421 / background_tasks.py ~3147): N replacement rows are appended
    and N old base rows deleted, leaving the row count unchanged at 300 with
    churn (10/300) far below the 0.2 index_rebuild_delta. Starting from
    FRESH bookkeeping, the whole swap neither awaits create_index with
    replace=True nor strands the bookkeeping stale for the next search.
    RED at base (the add leg retrains exactly as in the frozen node), GREEN
    after the fix.
    """
    row_state = {"total": 300}

    async def stateful_count_rows(filter_expr: str = ""):
        if filter_expr:
            # Rows matching the old-id delete predicate.
            return 10
        return row_state["total"]

    async def stateful_delete(filter_expr: str):
        row_state["total"] -= 10

    async def stateful_add(records):
        row_state["total"] += len(records)

    table = _make_mock_table(row_count=300, has_ivfpq=True)
    table.count_rows = AsyncMock(side_effect=stateful_count_rows)
    table.delete = AsyncMock(side_effect=stateful_delete)
    table.add = AsyncMock(side_effect=stateful_add)
    store = _fresh_store(table, row_count=300, tmp_path=tmp_path)

    records = [_record(i) for i in range(10)]
    old_ids = [f"1_old_{i}" for i in range(10)]

    with patch("app.services.vector_store.settings") as mock_settings, patch(
        "app.services.vector_store.IvfPq"
    ) as mock_ivfpq:
        mock_settings.optimize_mode = "manual"
        mock_settings.embedding_dim = 32
        mock_settings.vector_metric = "cosine"
        mock_settings.write_lock_timeout_seconds = 30
        mock_settings.index_rebuild_delta = 0.2
        mock_ivfpq.return_value = MagicMock()
        store._get_expected_embedding_dim = AsyncMock(return_value=4)

        await store.add_chunks_then_delete_ids(records, old_ids)

    # The swap itself happened: 10 rows appended, the old ids deleted.
    table.add.assert_awaited_once()
    table.delete.assert_awaited_once()
    assert row_state["total"] == 300

    # Net-zero churn against a fresh index: nothing retrains, on the append
    # leg or the delete leg.
    replace_calls = _replace_true_calls(table)
    assert not replace_calls, (
        "add_chunks_then_delete_ids with fresh bookkeeping and sub-delta "
        "churn must not retrain the IvfPq index, but create_index was "
        f"awaited with replace=True {len(replace_calls)} time(s): "
        f"{replace_calls}"
    )

    # And the next search() still finds the index fresh.
    assert await store._vector_index_needs_creation() is False, (
        "post-swap bookkeeping must stay coherent with the mutation "
        "generation, or the next search() retrains the index instead"
    )
