"""C01 equal-id-sets branch check (issue #706, PR #878 review PRR-001).

Equal id-sets (canonical and staging IDENTICAL) must take the DROP-STAGING
branch, not the destructive rebuild.

When both tables hold the same ids — the routine crash window between the
swap's final create and its staging drop — canonical is already complete.
The pre-fix first check was ``canonical_ids <= staging_ids``, which matched
on EQUAL sets too, so the reconcile dropped the healthy live table and
recreated it from staging: a full-table load plus a destructive drop/create
of a table that was already correct. The fix requires a STRICT subset for
the rebuild leg, resolving equal sets with the single cheap staging drop
base used ("dropped stale staging").

This test drives ``_reconcile_chunks_rebuild_state`` against a REAL
LanceDB store with two byte-identical generations and pins:

- staging is GONE afterwards (the drop ran);
- the log is the "dropped stale staging" INFO from the vector_store
  logger — NOT the rebuild branch's "restored incomplete" WARNING;
- canonical keeps its rows untouched.

Windows-safe (no symlinks), deterministic (no sleeps).
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
    _chunks_table_schema,
)

DIM = 8
_LOGGER_PREFIX = "app.services.vector_store"


def _row(chunk_id: str) -> dict:
    """A fully-populated canonical-schema row (identical on both sides)."""
    return {
        "id": chunk_id,
        "text": "identical generation row",
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
        # Multiples of 0.25 are exact in float32: the stored generation is
        # byte-stable across both tables.
        "embedding": [0.25 * (i + 1) for i in range(DIM)],
    }


@contextlib.contextmanager
def _swallow_schema_probe_coroutine_noise():  # noqa: ANN202
    """Keep any schema-probe RuntimeWarning inside the test.

    ``VectorStore._schema_embedding_dim`` awaits ``table.schema()`` when the
    backend returns a coroutine and accepts a plain schema object otherwise,
    so the probe is correct and should raise no warning; this simply stops
    one from ever reaching the repo-wide issue #565 session gate.
    tests/conftest.py explicitly exempts warnings captured inside a test's
    own ``warnings.catch_warnings(record=True)`` block from that gate.
    """
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always", RuntimeWarning)
        yield


def test_equal_id_sets_drop_staging_without_rebuild(tmp_path, caplog) -> None:
    """Identical tables on both sides → the cheap staging drop, never the
    destructive drop-and-recreate of the healthy live table (PRR-001)."""

    async def scenario(caplog):  # noqa: ANN001
        import shutil

        previous_data_dir = settings.data_dir
        settings.data_dir = tmp_path
        store = VectorStore(db_path=tmp_path / "lancedb")
        try:
            await store.connect()
            await store.init_table(DIM)
            await store.add_chunks([_row("e-1"), _row("e-2")])
            # Precondition: the seed really landed.
            seeded = await store.count_by_file("1")
            if seeded != 2:
                raise RuntimeError(
                    f"harness invalid: seeded index has {seeded} rows"
                )
            # A byte-identical leftover staging generation (the crashed-swap
            # window between the final create and the staging drop).
            await store.db.create_table(
                CHUNKS_STAGING_TABLE,
                data=pd.DataFrame([_row("e-1"), _row("e-2")]),
                schema=_chunks_table_schema(DIM),
            )

            raised: BaseException | None = None
            with caplog.at_level(logging.INFO, logger=_LOGGER_PREFIX):
                try:
                    with _swallow_schema_probe_coroutine_noise():
                        await store._reconcile_chunks_rebuild_state(
                            migration_name="c01 equal-sets probe"
                        )
                except BaseException as exc:  # noqa: BLE001
                    raised = exc

            names = set(await store.db.table_names())
            table = await store.db.open_table("chunks")
            ids_df = await table.query().select(["id"]).to_pandas()
            canonical_ids = set(ids_df["id"].astype(str))
            return raised, names, canonical_ids
        finally:
            settings.data_dir = previous_data_dir
            shutil.rmtree(tmp_path / "lancedb", ignore_errors=True)

    raised, names, canonical_ids = asyncio.run(scenario(caplog))

    assert raised is None
    # The drop ran: staging is gone, canonical keeps its rows.
    assert CHUNKS_STAGING_TABLE not in names
    assert canonical_ids == {"e-1", "e-2"}

    scoped = [
        record
        for record in caplog.records
        if record.name.startswith(_LOGGER_PREFIX)
    ]
    # ...announced by the cheap-drop INFO marker...
    assert any(
        record.levelno == logging.INFO
        and "dropped stale staging" in record.getMessage()
        for record in scoped
    ), "equal id-sets must resolve via the 'dropped stale staging' INFO"
    # ...and NOT by the destructive rebuild branch's marker.
    assert not any(
        "restored incomplete" in record.getMessage() for record in scoped
    ), "equal id-sets must NOT take the rebuild-from-staging branch"
