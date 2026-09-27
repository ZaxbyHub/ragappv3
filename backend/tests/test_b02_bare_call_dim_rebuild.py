"""B02 acceptance checks — bare-call dimension-rebuild wipes other files' vectors.

Frozen spec for the fix (RED at master; mirrors the harness in
``tests/issue513_checks/test_c9_reindex_dimension_migration.py``). When
``process_existing_file`` is called WITHOUT ``vector_target`` (the async
upload-worker path) and the file's embedding dimension differs from the live
LanceDB ``chunks`` table, the auto-migration commits a rebuild temp table
holding ONLY the current file's rows, so every OTHER file loses its vectors
while its SQLite ``files`` row still says ``status='indexed'``.

Contracts pinned:

1. ``test_other_files_vectors_survive_bare_call_dimension_change`` — the
   bare call must leave OTHER files' vectors retrievable (real LanceDB row
   counts); the new file's own ingest may legitimately fail post-fix.
2. ``test_no_indexed_file_left_without_vectors`` — no ``files`` row may
   claim ``status='indexed'`` while having zero vectors ('error'/'pending'
   rows are not orphans — only lying 'indexed' rows count).
3. ``test_scan_and_upload_paths_same_outcome_on_dimension_change`` — the
   scan entry point (``process_file``) and the upload entry point (bare
   ``process_existing_file``) must reach the SAME terminal ``files.status``
   for the ingested file on a dimension change. At master scan dies in
   ``add_chunks`` with ``VectorStoreValidationError`` ('error') while upload
   wipes the index and reports 'indexed'.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, patch

OLD_DIM = 4
NEW_DIM = 6


class _FakeEmbeddingService:
    """C9 contract double: ``embed_batch`` returns ``(vectors, failed_ids)``."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [
            [((i + 1) * 0.25) for i in range(self._dim)] for _ in texts
        ], []


def _record(rec_id: str, file_id: int, text: str, dim: int) -> dict:
    """One canonical LanceDB chunk row (C9 shape) at dimension ``dim``."""
    return {
        "id": rec_id,
        "text": text,
        "file_id": str(file_id),
        "vault_id": "1",
        "chunk_index": 0,
        "metadata": "{}",
        "embedding": [((i + 1) * 0.25) for i in range(dim)],
    }


@contextmanager
def _settings_at(tmp: Path) -> Iterator[None]:
    """Scope the ingest-gating settings patches to one isolated store."""
    from app.config import settings

    with patch.object(settings, "data_dir", tmp), \
            patch.object(settings, "wiki_enabled", False), \
            patch.object(settings, "wiki_compile_on_ingest", False), \
            patch.object(settings, "kms_enabled", False), \
            patch.object(settings, "multi_scale_indexing_enabled", False), \
            patch.object(settings, "contextual_chunking_enabled", False), \
            patch.object(settings, "optimize_mode", "manual"):
        yield


async def _seed_store(
    tmp: Path, tag: str, rows: list[tuple[str, str, str, int]]
) -> tuple[str, object, object, int, dict[str, int]]:
    """Build one isolated SQLite DB + LanceDB store under ``tmp``.

    ``rows`` holds ``(file_name, file_hash, status, n_seed_vectors)`` tuples;
    each becomes a ``files`` row at ``tmp / file_name`` plus ``n_seed_vectors``
    real OLD_DIM rows in the live ``chunks`` table. Distinct ``file_hash``
    values per row/store keep ``_check_duplicate`` out of the picture.
    """
    from app.models.database import get_pool, init_db
    from app.services.vector_store import VectorStore

    db_path = str(tmp / "app.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES (?)", (f"v_{tag}",))
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    ids: dict[str, int] = {}
    for file_name, file_hash, status, _ in rows:
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (vault_id, str(tmp / file_name), file_name, file_hash, 12, status),
        )
        ids[file_name] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    pool = get_pool(db_path, max_size=3)
    store = VectorStore(db_path=tmp / "lancedb")
    await store.init_table(OLD_DIM)
    for file_name, _, _, n_vecs in rows:
        fid = ids[file_name]
        await store.add_chunks(
            [
                _record(f"{tag}_{fid}_{i}", fid, f"seed {file_name} {i}", OLD_DIM)
                for i in range(n_vecs)
            ]
        )
    return db_path, pool, store, vault_id, ids


def _new_processor(store, pool, new_dim: int) -> object:  # noqa: ANN001
    """A DocumentProcessor wired to the fake NEW_DIM embedding service."""
    from app.services.document_processor import DocumentProcessor

    return DocumentProcessor(
        pool=pool,
        embedding_service=_FakeEmbeddingService(dim=new_dim),
        vector_store=store,
    )


async def _ingest(processor, *, upload: bool, **kwargs) -> None:  # noqa: ANN001
    """Run one ingest with parsing/hash/parent-windows stubbed (C9 pattern).

    ``upload=True`` drives the bare ``process_existing_file`` call the async
    upload worker makes (no ``vector_target``); ``upload=False`` drives the
    scan entry point ``process_file`` (which inserts its own ``files`` row).
    """
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument

    chunk = ProcessedChunk(
        text="ingest content produced with the new embedding model",
        metadata={"chunk_scale": "default", "raw_text": "ingest content"},
        chunk_index=0,
    )
    with patch.object(
        processor,
        "_process_document_file",
        new=AsyncMock(
            return_value=([chunk], "ingest content", ParsedDocument(atoms=()))
        ),
    ), patch(
        "app.services.document_processor.compute_file_hash",
        return_value=kwargs["fake_hash"],
    ), patch(
        "app.services.document_processor.compute_parent_windows"
    ):
        if upload:
            await processor.process_existing_file(
                file_id=kwargs["file_id"],
                file_path=str(kwargs["path"]),
                vault_id=kwargs["vault_id"],
            )
        else:
            await processor.process_file(
                str(kwargs["path"]), vault_id=kwargs["vault_id"], source="scan"
            )


async def _run_bare_upload_dimension_change(
    tmp: Path,
) -> tuple[str, object, dict[str, int]]:
    """Seed file A (indexed, 3 OLD_DIM vectors) + pending C, bare-call C at NEW_DIM.

    Mirrors the async upload worker: C's ``files`` row is the 'pending' row
    the upload route inserts and the call carries no ``vector_target``. The
    call is wrapped in try/except — post-fix C's ingest may legitimately be
    refused; the callers assert on the durable state it leaves behind.
    Returns ``(db_path, store, {file_name: files.id})``.
    """
    _db, pool, store, vault_id, ids = await _seed_store(
        tmp,
        "b02",
        [
            ("a.txt", "hash_b02_stays", "indexed", 3),
            ("c.txt", "hash_b02_arrives", "pending", 0),
        ],
    )
    upload_path = tmp / "c.txt"
    upload_path.write_text("upload content", encoding="utf-8")

    processor = _new_processor(store, pool, NEW_DIM)
    with _settings_at(tmp):
        try:
            await _ingest(
                processor,
                upload=True,
                file_id=ids["c.txt"],
                path=upload_path,
                vault_id=vault_id,
                fake_hash="hashb02barecall",
            )
        except Exception:  # noqa: BLE001 — C's ingest may legitimately fail post-fix
            pass
    return _db, store, ids


async def test_other_files_vectors_survive_bare_call_dimension_change(
    tmp_path: Path,
) -> None:
    """Bare upload-path call on a dim change must not wipe other files' vectors.

    File A is indexed with 3 live OLD_DIM vectors; file C is the pending row
    the async upload route inserts. A bare ``process_existing_file(C)`` with
    NEW_DIM embeddings must leave A's 3 vectors retrievable no matter how C's
    own ingest resolves. RED at master: A's count drops to 0.
    """
    tmp = tmp_path / "store"
    tmp.mkdir()
    _db, store, ids = await _run_bare_upload_dimension_change(tmp)

    n = await store.count_by_file(str(ids["a.txt"]))
    assert n == 3


async def test_no_indexed_file_left_without_vectors(tmp_path: Path) -> None:
    """After the bare call no 'indexed' row may have zero live vectors.

    Same scenario as the survival test; afterwards every ``files`` row that
    still claims ``status='indexed'`` must have at least one retrievable
    vector row. A refused ingest (C ending 'error') is fine — the contract
    bans only 'indexed' rows that lie. RED at master: A stays 'indexed' with
    0 vectors.
    """
    tmp = tmp_path / "store"
    tmp.mkdir()
    db_path, store, ids = await _run_bare_upload_dimension_change(tmp)

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id, status FROM files").fetchall()
    conn.close()
    orphans = 0
    for row_id, status in rows:
        if status == "indexed" and await store.count_by_file(str(row_id)) == 0:
            orphans += 1

    assert orphans == 0


async def test_scan_and_upload_paths_same_outcome_on_dimension_change(
    tmp_path: Path,
) -> None:
    """Scan and upload entry points must agree on the ingested row's status.

    Two fully isolated stores (own SQLite DB, own LanceDB dir, own data_dir),
    each seeded at OLD_DIM with one unrelated indexed vector row. The scan
    leg ingests via ``process_file(source="scan")``; the upload leg via a bare
    ``process_existing_file`` on its pending row — both with NEW_DIM fake
    embeddings. The ingested rows' terminal ``files.status`` must be
    identical. RED at master: scan 'error' vs upload 'indexed'.
    """
    # Leg 1: scan entry point (process_file inserts its own files row).
    scan_tmp = tmp_path / "scan_leg"
    scan_tmp.mkdir()
    _db1, pool1, store1, vault1, _ids1 = await _seed_store(
        scan_tmp,
        "b02scan",
        [("seeded_scan.txt", "hash_b02_seed_scan", "indexed", 1)],
    )
    scan_path = scan_tmp / "scan_doc.txt"
    scan_path.write_text("scan content", encoding="utf-8")

    processor1 = _new_processor(store1, pool1, NEW_DIM)
    with _settings_at(scan_tmp):
        try:
            await _ingest(
                processor1,
                upload=False,
                path=scan_path,
                vault_id=vault1,
                fake_hash="hashb02scanleg",
            )
        except Exception:  # noqa: BLE001 — either refusal or success is fine
            pass

    # Leg 2: bare upload entry point on its own pre-inserted pending row.
    up_tmp = tmp_path / "upload_leg"
    up_tmp.mkdir()
    db2, pool2, store2, vault2, ids2 = await _seed_store(
        up_tmp,
        "b02upload",
        [
            ("seeded_up.txt", "hash_b02_seed_upload", "indexed", 1),
            ("upload_doc.txt", "hash_b02_upload_pending", "pending", 0),
        ],
    )
    upload_path = up_tmp / "upload_doc.txt"
    upload_path.write_text("upload content", encoding="utf-8")

    processor2 = _new_processor(store2, pool2, NEW_DIM)
    with _settings_at(up_tmp):
        try:
            await _ingest(
                processor2,
                upload=True,
                file_id=ids2["upload_doc.txt"],
                path=upload_path,
                vault_id=vault2,
                fake_hash="hashb02uploadleg",
            )
        except Exception:  # noqa: BLE001 — either refusal or success is fine
            pass

    conn1 = sqlite3.connect(_db1)
    scan_status = conn1.execute(
        "SELECT status FROM files WHERE file_path = ?", (str(scan_path),)
    ).fetchone()[0]
    conn1.close()
    conn2 = sqlite3.connect(db2)
    upload_status = conn2.execute(
        "SELECT status FROM files WHERE id = ?", (ids2["upload_doc.txt"],)
    ).fetchone()[0]
    conn2.close()

    assert scan_status == upload_status
