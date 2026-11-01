"""C01 acceptance check (issue #706, vector-store migration safety).

Two concurrent dimension-changing reindex flows must both succeed.

Each flow mirrors the reindex job's faithful shape
(``background_tasks._reindex_embed_all`` — the ONLY production caller of
``begin_dimension_rebuild``):

  1. probe: ``embed_batch(["dimension_probe"], fail_fast=True)``;
  2. ``live_dim = await store.get_live_embedding_dim()``; when the probe
     dim differs, open ``begin_dimension_rebuild(NEW_DIM)``;
  3. ``process_existing_file(..., vector_target=handle)``;
  4. ``commit_dimension_rebuild(handle)``.

The probe awaits a shared 2-party ``asyncio.Barrier`` ONLY for the probe
texts — both flows rendezvous there, so both reach the begin decision
together and the race is deterministic (no sleeps/timing games). Per-file
embed calls never barrier: a barrier on every embed would deadlock any
serialization fix that holds a lock across the per-file phase.

At base the race is lost data: the ``rebuild_in_progress`` guard in
``begin_dimension_rebuild`` (vector_store.py:2510) is checked BEFORE the
write lock (:2516) is acquired, so two concurrent begins both pass it;
the second begin then DROPS the first's ``chunks_dim_rebuild`` table as
"stale", and the orphaned flow's vector write into the dropped table
fails — that file settles as 'error'. Acceptance: after both flows
settle, no file is in 'error' (each flow catches its own exception and
marks its file 'error', the way a failed job settles its files, so one
flow's failure never cancels the other before statuses settle).

The serving-pin resolution warning at rebuild open (sqlite settings_kv
sidecar absent) is harmless — the sqlite scenario mirrors
``tests/issue513_checks/test_c9_reindex_dimension_migration.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Repo root: this file lives at <repo>/backend/tests/<name>.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""


_hermetic_env()

OLD_DIM = 4
NEW_DIM = 6


class _BarrierEmbeddingService:
    """Fake embedding service at NEW_DIM.

    ``embed_batch`` rendezvous at the shared 2-party barrier ONLY when the
    texts are exactly the dimension-probe texts. fail_fast=True returns
    just the embeddings list (the job's probe contract); the default mode
    returns the (embeddings, failed) tuple the DocumentProcessor unpacks.
    """

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int, barrier: asyncio.Barrier) -> None:
        self._dim = dim
        self._barrier = barrier

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        if list(texts) == ["dimension_probe"]:
            await self._barrier.wait()
        embeddings = [
            [((i + 1) * 0.25) for i in range(self._dim)] for _ in texts
        ]
        if fail_fast:
            return embeddings
        return embeddings, []


def _record(rec_id: str, file_id: int, text: str, dim: int) -> dict:
    return {
        "id": rec_id,
        "text": text,
        "file_id": str(file_id),
        "vault_id": "1",
        "chunk_index": 0,
        "metadata": "{}",
        "embedding": [((i + 1) * 0.25) for i in range(dim)],
    }


def _cleanup_tmp(*paths) -> None:  # noqa: ANN001
    """Best-effort rmtree (Windows: pools/stores may hold open handles)."""
    import shutil

    for path in paths:
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)


async def _flow(
    store,
    pool,
    db_vault_id: int,
    file_id: int,
    upload_path: Path,
    db_path: str,
    barrier: asyncio.Barrier,
):  # noqa: ANN001, ANN202
    """One reindex-job-shaped dimension migration flow for one file.

    Catches its OWN exception, settles its file as 'error' (the way a
    failed job settles its files) and returns the exception — one flow's
    failure must not cancel the other before statuses settle.
    """
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument
    from app.services.document_processor import DocumentProcessor

    embedding_service = _BarrierEmbeddingService(NEW_DIM, barrier)
    handle = None
    try:
        # 1. probe (both flows rendezvous at the barrier here).
        probe_embeddings = await embedding_service.embed_batch(
            ["dimension_probe"], fail_fast=True
        )
        probe_dim = (
            len(probe_embeddings[0])
            if probe_embeddings and probe_embeddings[0] is not None
            else None
        )
        # 2. open a staged rebuild when the dimension changed.
        live_dim = await store.get_live_embedding_dim()
        if probe_dim != live_dim:
            handle = await store.begin_dimension_rebuild(probe_dim)
        # 3. reprocess through the rebuild target.
        chunk = ProcessedChunk(
            text="reindexed content with the new embedding model",
            metadata={"chunk_scale": "default", "raw_text": "reindexed content"},
            chunk_index=0,
        )
        processor = DocumentProcessor(
            pool=pool,
            embedding_service=embedding_service,
            vector_store=store,
        )
        with patch.object(
            processor,
            "_process_document_file",
            new=AsyncMock(
                return_value=(
                    [chunk],
                    "reindexed content",
                    ParsedDocument(atoms=()),
                )
            ),
        ):
            await processor.process_existing_file(
                file_id=file_id,
                file_path=str(upload_path),
                vault_id=db_vault_id,
                vector_target=handle,
            )
        # 4. commit the staged rebuild.
        if handle is not None:
            await store.commit_dimension_rebuild(handle)
        return None
    except Exception as exc:  # noqa: BLE001 — settle the file, don't cancel peers
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE files SET status = 'error', error_message = "
                "'concurrent rebuild flow failed' WHERE id = ?",
                (file_id,),
            )
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass
        return exc


async def _scenario() -> int:
    """Returns the number of the two files settled as 'error'."""
    from app.config import settings
    from app.models.database import get_pool, init_db, run_migrations
    from app.services.vector_store import VectorStore

    tmp = Path(tempfile.mkdtemp(prefix="c01_conc_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)
    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    for name, seed_hash in (
        ("docA.txt", "hashc01fileA"),
        ("docB.txt", "hashc01fileB"),
    ):
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (?, ?, ?, ?, ?, 'indexed')",
            (vault_id, str(tmp / name), name, seed_hash, 12),
        )
    conn.commit()
    file_rows = conn.execute(
        "SELECT id, file_path FROM files ORDER BY id"
    ).fetchall()
    conn.close()
    file_ids = [row[0] for row in file_rows]
    upload_paths = [Path(row[1]) for row in file_rows]
    for upload_path in upload_paths:
        upload_path.write_text("seed content", encoding="utf-8")

    pool = get_pool(db_path, max_size=4)
    store = VectorStore(db_path=tmp / "lancedb")

    with (
        patch.object(settings, "data_dir", tmp),
        patch.object(settings, "wiki_enabled", False),
        patch.object(settings, "wiki_compile_on_ingest", False),
        patch.object(settings, "kms_enabled", False),
        patch.object(settings, "multi_scale_indexing_enabled", False),
        patch.object(settings, "contextual_chunking_enabled", False),
        patch.object(settings, "optimize_mode", "manual"),
        # Flow-independent module-level patches hoisted around the gather
        # (the c9 _reprocess_file patches): concurrent per-flow patches of
        # the same attribute would restore each other mid-flight.
        patch(
            "app.services.document_processor.compute_file_hash",
            return_value="hashc01file",
        ),
        patch("app.services.document_processor.compute_parent_windows"),
    ):
        # Seed the old-dimension native index with both files' vectors.
        await store.init_table(OLD_DIM)
        await store.add_chunks(
            [
                _record(f"{file_ids[0]}_0", file_ids[0], "seed content A", OLD_DIM),
                _record(f"{file_ids[1]}_0", file_ids[1], "seed content B", OLD_DIM),
            ]
        )
        seeded = await store.count_by_file(str(file_ids[0]))
        seeded += await store.count_by_file(str(file_ids[1]))
        if seeded != 2:
            _cleanup_tmp(tmp)
            raise RuntimeError(f"harness invalid: seeded index has {seeded} rows")

        barrier = asyncio.Barrier(2)
        results = await asyncio.wait_for(
            asyncio.gather(
                _flow(
                    store, pool, vault_id, file_ids[0], upload_paths[0],
                    db_path, barrier,
                ),
                _flow(
                    store, pool, vault_id, file_ids[1], upload_paths[1],
                    db_path, barrier,
                ),
                return_exceptions=True,
            ),
            60,
        )

    conn = sqlite3.connect(db_path)
    statuses = [
        row[0]
        for row in conn.execute(
            "SELECT status FROM files WHERE id IN (?, ?) ORDER BY id",
            (file_ids[0], file_ids[1]),
        ).fetchall()
    ]
    conn.close()

    # The live table must still hold rows for BOTH files (PRR-016): a run
    # whose files.status says 'indexed' while one file's vectors were
    # silently wiped is not a pass. Read before cleanup (Windows keeps
    # handles open past rmtree).
    live_rows_per_file = [
        await store.count_by_file(str(file_id)) for file_id in file_ids
    ]
    _cleanup_tmp(tmp)

    if len(statuses) != 2:
        raise RuntimeError(f"harness invalid: statuses read {statuses}")
    error_count = sum(1 for status in statuses if status == "error")
    # Each flow catches its own exception and settles its file as 'error',
    # so the returned exceptions and the statuses agree by construction;
    # the verdict is the settled status of both files — AND (PRR-022) the
    # gathered outcomes themselves, which the caller now asserts instead of
    # discarding: a raise whose error-settling UPDATE also failed cannot
    # slip through on statuses alone.
    return error_count, live_rows_per_file, list(results), statuses


def test_two_concurrent_ingests_across_dimension_change_both_succeed() -> None:
    error_count, live_rows_per_file, results, statuses = asyncio.run(_scenario())
    assert error_count == 0
    # PRR-016: neither file's vectors were silently wiped — the live table
    # holds rows for BOTH file ids after the concurrent rebuild.
    assert len(live_rows_per_file) == 2
    assert all(
        rows >= 1 for rows in live_rows_per_file
    ), f"live 'chunks' lost a file's vectors: {live_rows_per_file}"
    # PRR-022: every gathered outcome is either success (None) or a flow
    # whose file settled 'error' — a raise plus failed bookkeeping fails.
    for outcome, status in zip(results, statuses):
        if outcome is not None:
            assert status == "error", (
                f"flow raised {outcome!r} but its file settled {status!r}"
            )
