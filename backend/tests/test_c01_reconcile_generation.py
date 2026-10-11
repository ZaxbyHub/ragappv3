"""C01 generation-divergence checks (issue #706, PR #878 review PRR-002).

The reconcile now compares vector GENERATION, not just ids and text.

Chunk ids are deterministic, so a stale ``chunks_rebuild`` left over from an
earlier embedding generation can match the live ``chunks`` table on every id
AND every text cell while holding previous-model vectors. Accepting it (the
pre-fix subset logic rebuilt canonical from staging whenever
``canonical_ids <= staging_ids``) would silently swap wrong-generation
vectors back under a sidecar that already records the new generation.

These tests drive ``_reconcile_chunks_rebuild_state`` against a REAL
LanceDB store (the projection paths — ``query().where().select()`` — and
the stored float32 embeddings are the point):

- same ids + same text + DIFFERENT embedding values  → DIVERGENT
  (CRITICAL + VectorStoreError, both tables preserved);
- same ids + same text + DIFFERENT embedding width   → DIVERGENT (same);
- canonical a STRICT subset with identical ids/text/embeddings → the
  rebuild-from-staging branch still runs (negative control: the generation
  check must not fire on a genuinely matching generation).

Windows-safe (no symlinks), deterministic (no sleeps; identical float32
lattice values on both sides).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""


_hermetic_env()

import pandas as pd  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.vector_store import (  # noqa: E402
    CHUNKS_STAGING_TABLE,
    VectorStore,
    VectorStoreError,
    _chunks_table_schema,
)

DIM = 8
_LOGGER_PREFIX = "app.services.vector_store"


def _gen_record(chunk_id: str, dim: int = DIM, text: str = "same text", value: float = 0.25) -> dict:
    """A fully-populated canonical-schema row; ``value`` scales the embedding
    lattice (multiples of 0.25 are exactly representable in float32, so
    equal-in / equal-out and different-in / different-out are both stable)."""
    return {
        "id": chunk_id,
        "text": text,
        "file_id": "1",
        "vault_id": "0",
        "chunk_index": 0,
        "chunk_scale": "default",
        "sparse_embedding": None,
        "metadata": "{}",
        "parent_doc_id": "1",
        "parent_window_start": 0,
        "parent_window_end": 11,
        "chunk_position": 0,
        "embedding": [value * (i + 1) for i in range(dim)],
    }


async def _table_ids(store: VectorStore, name: str) -> set:
    table = await store.db.open_table(name)
    df = await table.query().select(["id"]).to_pandas()
    return set(df["id"].astype(str))


async def _build(tmp: Path, canonical_rows: list[dict], staging_rows: list[dict], staging_dim: int) -> VectorStore:
    settings.data_dir = tmp
    store = VectorStore(db_path=tmp / "lancedb")
    await store.connect()
    await store.init_table(DIM)
    await store.add_chunks(canonical_rows)
    # Staging with the EXPLICIT fixed-width schema (the shape a crashed swap
    # leaves behind; commit_dimension_rebuild creates it the same way).
    await store.db.create_table(
        CHUNKS_STAGING_TABLE,
        data=pd.DataFrame(staging_rows),
        schema=_chunks_table_schema(staging_dim),
    )
    return store


async def _drive(tmp: Path, canonical_rows, staging_rows, staging_dim=DIM):  # noqa: ANN001, ANN202
    """Run the reconcile; returns (raised, table_names, canonical_ids, staging_ids)."""
    import shutil

    previous_data_dir = settings.data_dir
    store = await _build(tmp, canonical_rows, staging_rows, staging_dim)
    try:
        raised: BaseException | None = None
        try:
            with _swallow_schema_probe_coroutine_noise():
                await store._reconcile_chunks_rebuild_state(
                    migration_name="c01 generation probe"
                )
        except BaseException as exc:  # noqa: BLE001
            raised = exc
        names = set(await store.db.table_names())
        canonical_ids = await _table_ids(store, "chunks")
        staging_ids = (
            await _table_ids(store, CHUNKS_STAGING_TABLE)
            if CHUNKS_STAGING_TABLE in names
            else None
        )
        return raised, names, canonical_ids, staging_ids
    finally:
        settings.data_dir = previous_data_dir
        shutil.rmtree(tmp / "lancedb", ignore_errors=True)


def _scoped_records(caplog, minimum_level: int) -> list:  # noqa: ANN001
    return [
        record
        for record in caplog.records
        if record.levelno >= minimum_level
        and record.name.startswith(_LOGGER_PREFIX)
    ]


@contextlib.contextmanager
def _swallow_schema_probe_coroutine_noise():  # noqa: ANN202
    """Capture schema-probe RuntimeWarnings locally.

    ``VectorStore._schema_embedding_dim`` awaits ``table.schema()`` when the
    backend returns a coroutine, but a double that returns a plain schema
    object is also accepted. This helper simply keeps any warning the probe
    raises inside the test rather than letting it reach the repo-wide
    issue #565 session gate; the probe itself is correct, so no warning
    should be raised at all.

    tests/conftest.py explicitly exempts warnings captured inside a test's
    own ``warnings.catch_warnings(record=True)`` block from the #565 gate,
    so this uses exactly that mechanism.
    """
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always", RuntimeWarning)
        yield


def test_same_ids_and_text_but_different_embedding_values_is_divergent(
    tmp_path, caplog
) -> None:
    """Identical ids + identical text + different vectors must fail closed
    (PRR-002): the stale-generation staging copy is NOT swapped back in."""

    async def scenario(caplog):  # noqa: ANN001
        with caplog.at_level(logging.CRITICAL, logger=_LOGGER_PREFIX):
            return await _drive(
                tmp_path,
                canonical_rows=[_gen_record("g-1"), _gen_record("g-2")],
                staging_rows=[
                    _gen_record("g-1", value=0.5),
                    _gen_record("g-2", value=0.5),
                ],
            )

    raised, names, canonical_ids, staging_ids = asyncio.run(scenario(caplog))
    assert raised is not None and isinstance(raised, VectorStoreError)
    assert any(
        record.levelno >= logging.CRITICAL for record in _scoped_records(caplog, logging.CRITICAL)
    ), "generation divergence must log CRITICAL from the vector_store logger"
    # NEITHER table was modified: both preserved with their original rows.
    assert "chunks" in names
    assert CHUNKS_STAGING_TABLE in names
    assert canonical_ids == {"g-1", "g-2"}
    assert staging_ids == {"g-1", "g-2"}


def test_same_ids_and_text_but_different_embedding_width_is_divergent(
    tmp_path, caplog
) -> None:
    """A staging copy from a different DIMENSION (previous-width vectors)
    is a different generation even when ids and text all match."""

    async def scenario(caplog):  # noqa: ANN001
        with caplog.at_level(logging.CRITICAL, logger=_LOGGER_PREFIX):
            return await _drive(
                tmp_path,
                canonical_rows=[_gen_record("g-1"), _gen_record("g-2")],
                staging_rows=[_gen_record("g-1", dim=4), _gen_record("g-2", dim=4)],
                staging_dim=4,
            )

    raised, names, canonical_ids, staging_ids = asyncio.run(scenario(caplog))
    assert raised is not None and isinstance(raised, VectorStoreError)
    assert any(
        record.levelno >= logging.CRITICAL for record in _scoped_records(caplog, logging.CRITICAL)
    )
    assert "chunks" in names
    assert CHUNKS_STAGING_TABLE in names
    assert canonical_ids == {"g-1", "g-2"}
    assert staging_ids == {"g-1", "g-2"}


def test_identical_generation_strict_subset_still_rebuilds_from_staging(
    tmp_path, caplog
) -> None:
    """Negative control: identical ids/text/embeddings with canonical a
    STRICT subset of staging keeps the recovery rebuild — the generation
    check must not fire on a genuinely matching generation."""

    async def scenario(caplog):  # noqa: ANN001
        with caplog.at_level(logging.INFO, logger=_LOGGER_PREFIX):
            return await _drive(
                tmp_path,
                canonical_rows=[_gen_record("g-1")],
                staging_rows=[_gen_record("g-1"), _gen_record("g-2")],
            )

    raised, names, canonical_ids, staging_ids = asyncio.run(scenario(caplog))
    assert raised is None
    # The rebuild branch ran to completion: staging consumed, canonical now
    # holds every staging row.
    assert CHUNKS_STAGING_TABLE not in names
    assert canonical_ids == {"g-1", "g-2"}
    assert staging_ids is None
    assert any(
        "restored incomplete" in record.getMessage()
        for record in _scoped_records(caplog, logging.INFO)
    ), "the strict-subset recovery must still announce the restore"
