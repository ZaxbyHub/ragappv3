"""Real-LanceDB integration tests for FTS index detection (issue #557).

These tests drive the REAL lancedb async engine (``lancedb.connect_async`` on a
per-test temp directory — the locked lancedb 0.36.0) through the REAL
production entry points: ``VectorStore.init_table`` (which owns the
create-if-missing FTS guard in ``_init_table_unlocked``) and
``app.lifespan.validate_fts_index`` (the startup validator). There are NO
mocks on the table or the database connection anywhere in this module.

Why this exists: both FTS detection sites check the index by a hardcoded NAME
that no production code ever passes to ``create_index``. The application's only
FTS creation path is ``table.create_index(column="text", config=FTS(),
replace=False)`` — no ``name=`` — so LanceDB auto-names the index ``text_idx``
(runtime-proven on the locked 0.36.0: ``index_type='FTS'``,
``columns=['text']``). The guard is therefore always False, and on every boot
after the first the re-creation attempt raises "Index name 'text_idx' already
exists", producing a false WARNING ("FTS index creation failed") and a false
ERROR ("FTS index is missing"). Detection must instead be column+type based.

Each assertion message carries a ``C<n>:`` sentinel prefix so a base-run
failure is identifiable by regex. Expected pre-fix outcomes: C1 FAIL, C2 FAIL,
C4 PASS, C5 PASS.
"""

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Sequence

from lancedb.index import IvfPq

from app.lifespan import validate_fts_index
from app.services.vector_store import VectorStore

EMBEDDING_DIM = 384
_SEED_ROWS = 260  # >= VECTOR_INDEX_MIN_ROWS (256) so the ANN paths engage

# vector_store.py and lifespan.py both use ``logging.getLogger(__name__)``.
VECTOR_STORE_LOGGER = "app.services.vector_store"
LIFESPAN_LOGGER = "app.lifespan"


class _CapturingHandler(logging.Handler):
    """Handler that collects every record emitted while it is attached."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_logger(*names: str) -> Iterator[List[_CapturingHandler]]:
    """Capture records emitted to the named application loggers.

    A capturing handler is attached DIRECTLY to each named logger (not to the
    root logger), and each logger's own level is lowered to DEBUG for the
    capture window so INFO records reach the handler even while the root logger
    sits at its default WARNING. Both the level and the handler are restored on
    exit.
    """
    attached: List = []  # (logger, previous_level, handler)
    try:
        for name in names:
            lg = logging.getLogger(name)
            handler = _CapturingHandler()
            attached.append((lg, lg.level, handler))
            lg.setLevel(logging.DEBUG)
            lg.addHandler(handler)
        yield [entry[2] for entry in attached]
    finally:
        for lg, previous_level, handler in attached:
            lg.removeHandler(handler)
            lg.setLevel(previous_level)


def _messages(
    records: Sequence[logging.LogRecord],
    *,
    levelname: str = None,
    containing: str = None,
) -> List[str]:
    """Return the formatted messages of records matching the filters."""
    out: List[str] = []
    for record in records:
        if levelname is not None and record.levelname != levelname:
            continue
        message = record.getMessage()
        if containing is not None and containing not in message:
            continue
        out.append(message)
    return out


def _describe(indices) -> str:
    """Render an index listing for assertion messages."""
    return repr(
        [
            (idx.name, str(getattr(idx, "index_type", None)), list(idx.columns))
            for idx in indices
        ]
    )


def _chunk_row(i: int) -> Dict:
    """Build one row satisfying every NOT NULL column of the chunks schema.

    ``_chunks_table_schema`` (backend/app/services/vector_store.py) requires
    id, text, file_id, vault_id, chunk_index, chunk_scale, sparse_embedding,
    metadata and the fixed-width embedding list; the parent_* and
    chunk_position columns are nullable. The embedding is deterministic but
    varied across rows and dimensions so ANN training sees non-degenerate
    vectors.
    """
    return {
        "id": f"chunk-{i:05d}",
        "text": f"integration chunk {i} alpha bravo charlie delta echo",
        "file_id": f"file-{i:04d}",
        "vault_id": "vault-integration",
        "chunk_index": i,
        "chunk_scale": "512",
        "sparse_embedding": "",
        "metadata": "{}",
        "parent_doc_id": None,
        "parent_window_start": None,
        "parent_window_end": None,
        "chunk_position": None,
        "embedding": [((i * 7 + j) % 13) / 13.0 for j in range(EMBEDDING_DIM)],
    }


async def _connected_store(tmp_path: Path) -> VectorStore:
    """A VectorStore connected to a fresh LanceDB dir under tmp_path."""
    store = VectorStore(db_path=tmp_path / "lancedb")
    await store.connect()
    return store


async def test_second_init_logs_no_fake_warning(tmp_path: Path) -> None:
    """Boot 2+: init_table on an existing table must not warn about FTS.

    The create-if-missing guard exists precisely so the second boot skips
    re-creation. Detection by a name no production path ever sets makes the
    guard dead: the second create_index raises "Index name 'text_idx' already
    exists", which is caught and re-reported as the false WARNING
    "FTS index creation failed (hybrid search will be unavailable)".
    """
    store = await _connected_store(tmp_path)
    with capture_logger(VECTOR_STORE_LOGGER) as (cap,):
        await store.init_table(embedding_dim=EMBEDDING_DIM)
        cap.records.clear()  # only the SECOND init's records matter below
        await store.init_table(embedding_dim=EMBEDDING_DIM)
    fake_warnings = _messages(
        cap.records, levelname="WARNING", containing="FTS index creation failed"
    )
    assert not fake_warnings, (
        f"C1: second init_table on an existing table must not log the false "
        f"'FTS index creation failed' WARNING (the FTS index already exists): "
        f"{fake_warnings}"
    )


async def test_validate_fts_index_returns_true_on_real_table(tmp_path: Path) -> None:
    """lifespan validation must accept the real auto-named FTS index.

    After one real init_table the table carries the real FTS index on the
    'text' column; startup validation must return True and must NOT log the
    false ERROR 'Hybrid search is enabled but the FTS index is missing'.
    """
    store = await _connected_store(tmp_path)
    await store.init_table(embedding_dim=EMBEDDING_DIM)
    with capture_logger(LIFESPAN_LOGGER) as (cap,):
        result = await validate_fts_index(store.table)
    assert result is True, (
        f"C2: validate_fts_index must return True for a real table whose FTS "
        f"index exists; got {result!r} for indices "
        f"{_describe(await store.table.list_indices())}"
    )
    false_errors = _messages(
        cap.records, levelname="ERROR", containing="FTS index is missing"
    )
    assert not false_errors, (
        f"C2: validate_fts_index must not log the false 'FTS index is missing' "
        f"ERROR when the FTS index is present: {false_errors}"
    )


async def test_fresh_table_creates_fts_index_once_no_warning(tmp_path: Path) -> None:
    """Boot 1 on a fresh dir: exactly one real FTS index, INFO log, no noise.

    Pins how the real engine reports the index created by the production
    create path: a single index with columns == ['text'] and
    index_type == 'FTS' (auto-named 'text_idx' on lancedb 0.36.0). This is
    the shape column+type detection must match, and the noise-free baseline
    the second boot must also reach.
    """
    store = await _connected_store(tmp_path)
    with capture_logger(VECTOR_STORE_LOGGER) as (cap,):
        await store.init_table(embedding_dim=EMBEDDING_DIM)
    indices = await store.table.list_indices()
    fts_indices = [
        idx
        for idx in indices
        if list(getattr(idx, "columns", None) or []) == ["text"]
        and str(getattr(idx, "index_type", None)) == "FTS"
    ]
    assert len(fts_indices) == 1, (
        f"C4: exactly one FTS index (columns == ['text'], index_type == 'FTS') "
        f"must exist after the first init_table, got {_describe(indices)}"
    )
    created_infos = _messages(
        cap.records,
        levelname="INFO",
        containing="Full-text search index created on 'text' column",
    )
    assert created_infos, (
        f"C4: first init_table must log INFO \"Full-text search index created "
        f"on 'text' column\"; captured: "
        f"{_messages(cap.records, levelname='INFO')}"
    )
    fts_noise = [
        (record.levelname, record.getMessage())
        for record in cap.records
        if record.levelname in ("WARNING", "ERROR") and "FTS" in record.getMessage()
    ]
    assert not fts_noise, (
        f"C4: a fresh table's init_table must not emit any WARNING/ERROR "
        f"mentioning FTS: {fts_noise}"
    )


async def test_ann_seed_baseline_from_real_ivfpq_index(tmp_path: Path) -> None:
    """Re-init over a REAL IvfPq index must seed the ANN churn baseline.

    Scope guard for the shared-helper refactor: the ANN index-existence check
    in ``_init_table_unlocked`` must keep working. A real IVF_PQ index created
    with ``create_index(column='embedding', config=IvfPq(...))`` reports
    ``columns=['embedding']``, ``index_type='IvfPq'`` (auto-named
    'embedding_idx' on lancedb 0.36.0) — so this test must stay GREEN both
    BEFORE and AFTER the FTS detection fix. num_sub_vectors=48 divides the
    384-dim embedding width; KMeans warnings from the lance logger on this
    small dataset are harmless.
    """
    store = await _connected_store(tmp_path)
    await store.init_table(embedding_dim=EMBEDDING_DIM)

    # Grow the real chunks table past the ANN threshold with schema-valid rows.
    await store.table.add([_chunk_row(i) for i in range(_SEED_ROWS)])

    # Real ANN index on the embedding column, exactly like production creates.
    await store.table.create_index(
        column="embedding",
        config=IvfPq(num_partitions=2, num_sub_vectors=48),
        replace=False,
    )

    # Existing-table path: the ANN check must detect the index and seed the
    # churn baseline from the live row count.
    await store.init_table(embedding_dim=EMBEDDING_DIM)
    row_count = await store.table.count_rows()
    assert (
        store._last_index_build_row_count == row_count and row_count >= _SEED_ROWS
    ), (
        f"C5: re-init over a real IVF_PQ index must seed "
        f"_last_index_build_row_count from the table row count (expected "
        f"{row_count} rows, got baseline {store._last_index_build_row_count})"
    )
