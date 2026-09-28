"""B02 guardrail — the dimension-compatibility probe's full branch contract.

Unit-level companion to ``test_b02_bare_call_dim_rebuild.py`` (issue #691):
``DocumentProcessor._ensure_live_dimension_compatible`` replaced the bare-call
dimension auto-migration, so every branch of the probe is pinned here:

- a CONFIRMED mismatch raises ``DocumentProcessingError`` naming both
  dimensions and the reindex remediation, and never opens a
  ``chunks_dim_rebuild`` rebuild table (the #691 data-loss mechanism);
- matching dimensions, a missing live table, a store without the probe API,
  ``vector_store=None``, a failing probe, and a non-numeric probe return all
  proceed without raising (the ordinary write path still rejects a real
  mismatch).

Demonstrated RED-on-revert for the mismatch branch during the #691 trace
(reverting the fix restores the auto-migration, which opens the rebuild table
and raises nothing).
"""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


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


def _processor(pool, store) -> object:
    from app.services.document_processor import DocumentProcessor

    class _NoEmbeddings:
        MAX_TEXT_LENGTH = 8192
        embedding_doc_prefix = ""

        async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
            return [[0.25] for _ in texts], []

    return DocumentProcessor(pool=pool, embedding_service=_NoEmbeddings(), vector_store=store)


async def _seeded_store(tmp_path: Path, dim: int):
    from app.models.database import get_pool, init_db
    from app.services.vector_store import VectorStore

    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    conn.commit()
    conn.close()
    pool = get_pool(db_path, max_size=2)
    store = VectorStore(db_path=tmp_path / "lancedb")
    await store.init_table(dim)
    await store.add_chunks([_record("grd_0", 1, "seed", dim)])
    return pool, store


async def test_mismatch_raises_actionable_error_without_rebuild_table(
    tmp_path: Path,
) -> None:
    """A confirmed dimension mismatch refuses with dims + reindex guidance.

    The refusal must NOT open the staged rebuild table — opening it was the
    #691 wipe mechanism.
    """
    from app.services.document_processor import DocumentProcessingError
    from app.services.vector_store import DIMENSION_REBUILD_TABLE

    pool, store = await _seeded_store(tmp_path, 4)
    processor = _processor(pool, store)

    with pytest.raises(DocumentProcessingError) as excinfo:
        await processor._ensure_live_dimension_compatible(6)

    message = str(excinfo.value)
    assert "dimension 6" in message
    assert "dimension 4" in message
    assert "reindex" in message.lower()

    table_names = await store.db.table_names()
    assert DIMENSION_REBUILD_TABLE not in table_names


async def test_matching_dimension_proceeds(tmp_path: Path) -> None:
    pool, store = await _seeded_store(tmp_path, 6)
    processor = _processor(pool, store)
    await processor._ensure_live_dimension_compatible(6)


async def test_missing_live_table_proceeds(tmp_path: Path) -> None:
    from app.models.database import get_pool, init_db
    from app.services.vector_store import VectorStore

    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    processor = _processor(get_pool(db_path, max_size=2), VectorStore(db_path=tmp_path / "lancedb"))
    # No live table yet: the first ingest creates it at the incoming dim.
    await processor._ensure_live_dimension_compatible(6)


async def test_store_without_probe_api_proceeds(tmp_path: Path) -> None:
    """Test doubles / alternative backends without the probe are skipped."""
    from app.models.database import get_pool, init_db

    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    bare_store = SimpleNamespace()  # no get_live_embedding_dim attribute
    processor = _processor(get_pool(db_path, max_size=2), bare_store)
    await processor._ensure_live_dimension_compatible(6)


async def test_none_vector_store_proceeds(tmp_path: Path) -> None:
    from app.models.database import get_pool, init_db
    from app.services.document_processor import DocumentProcessor

    db_path = str(tmp_path / "app.db")
    init_db(db_path)

    class _NoEmbeddings:
        MAX_TEXT_LENGTH = 8192
        embedding_doc_prefix = ""

        async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
            return [[0.25] for _ in texts], []

    processor = DocumentProcessor(
        pool=get_pool(db_path, max_size=2),
        embedding_service=_NoEmbeddings(),
        vector_store=None,
    )
    await processor._ensure_live_dimension_compatible(6)


async def test_failing_probe_proceeds(tmp_path: Path) -> None:
    """A transient probe failure logs and proceeds (best-effort contract)."""

    class _FailingStore:
        async def get_live_embedding_dim(self) -> int:
            raise RuntimeError("transient store failure")

    from app.models.database import get_pool, init_db

    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    processor = _processor(get_pool(db_path, max_size=2), _FailingStore())
    await processor._ensure_live_dimension_compatible(6)


async def test_non_numeric_probe_return_proceeds(tmp_path: Path) -> None:
    """An unfaithful double returning a non-numeric dim must not crash."""
    from app.models.database import get_pool, init_db

    class _BadDimStore:
        async def get_live_embedding_dim(self):  # noqa: ANN202
            return "not-a-number"

    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    processor = _processor(get_pool(db_path, max_size=2), _BadDimStore())
    await processor._ensure_live_dimension_compatible(6)


# --- Review-round wiring pins (issue #691 implementation review) ------------
#
# The two tests below pin the END-TO-END wiring the unit tests above cannot
# see: the scan entry point must refuse BEFORE any vector-store call (its
# old failure mode was a mid-write VectorStoreValidationError), and the
# persisted user-visible error must be the actionable DIMENSION_CHANGED code
# — not the generic PARSE_FAILED reason the redaction layer used to emit.

def _scan_harness_patches(processor, tmp: Path, fake_hash: str):
    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument

    chunk = ProcessedChunk(
        text="scan content at the new dimension",
        metadata={"chunk_scale": "default", "raw_text": "scan content"},
        chunk_index=0,
    )
    return (
        patch.object(settings, "data_dir", tmp),
        patch.object(settings, "wiki_enabled", False),
        patch.object(settings, "wiki_compile_on_ingest", False),
        patch.object(settings, "kms_enabled", False),
        patch.object(settings, "multi_scale_indexing_enabled", False),
        patch.object(settings, "contextual_chunking_enabled", False),
        patch.object(settings, "optimize_mode", "manual"),
        patch.object(
            processor,
            "_process_document_file",
            new=AsyncMock(
                return_value=([chunk], "scan content", ParsedDocument(atoms=()))
            ),
        ),
        patch(
            "app.services.document_processor.compute_file_hash",
            return_value=fake_hash,
        ),
        patch("app.services.document_processor.compute_parent_windows"),
    )


async def test_scan_probe_refuses_before_any_write(tmp_path: Path) -> None:
    """The scan entry point refuses a dimension change BEFORE any vector call.

    Deleting the probe line in ``process_file`` must fail this test: without
    it the ingest dies mid-write inside ``add_chunks`` instead of failing
    closed up front (issue #691 review, scan-wiring pin).
    """
    from app.services.document_processor import (
        DocumentProcessingError,
        EmbeddingDimensionChangedError,
    )

    pool, store = await _seeded_store(tmp_path, 4)
    processor = _processor(pool, store)

    # Spies: the refusal must leave the vector store untouched.
    calls: list[dict] = []
    orig_init = store.init_table
    orig_add = store.add_chunks

    async def spy_init(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append({"method": "init_table", **kwargs})
        return await orig_init(*args, **kwargs)

    async def spy_add(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append({"method": "add_chunks", **kwargs})
        return await orig_add(*args, **kwargs)

    store.init_table = spy_init  # type: ignore[method-assign]
    store.add_chunks = spy_add  # type: ignore[method-assign]

    scan_path = tmp_path / "scan_doc.txt"
    scan_path.write_text("scan content", encoding="utf-8")

    patches = _scan_harness_patches(processor, tmp_path, "hashgrdscan")
    try:
        with ExitStack() as stack:
            for cm in patches:
                stack.enter_context(cm)
            with pytest.raises(EmbeddingDimensionChangedError) as excinfo:
                await processor.process_file(
                    str(scan_path), vault_id=1, source="scan"
                )
    finally:
        store.init_table = orig_init  # type: ignore[method-assign]
        store.add_chunks = orig_add  # type: ignore[method-assign]

    assert isinstance(excinfo.value, DocumentProcessingError)
    assert calls == [], f"vector store was touched before the refusal: {calls}"
    # The unrelated seeded file's vectors are intact.
    assert await store.count_by_file("1") == 1

    # The persisted user-visible error is the actionable stable code.
    conn = sqlite3.connect(str(tmp_path / "app.db"))
    row = conn.execute(
        "SELECT status, error_message FROM files WHERE file_path = ?",
        (str(scan_path),),
    ).fetchone()
    conn.close()
    assert row is not None and row[0] == "error"
    assert row[1].startswith("DIMENSION_CHANGED:"), row[1]


async def test_upload_refusal_persists_dimension_changed_code(
    tmp_path: Path,
) -> None:
    """The upload-path refusal persists the actionable code, not PARSE_FAILED.

    The raw exception (with both dimensions) stays in the server log; the
    persisted ``files.error_message`` — the field the UI renders — must be
    the stable DIMENSION_CHANGED code with the reindex remediation
    (issue #691 review, persistence pin).
    """
    from app.services.document_processor import EmbeddingDimensionChangedError

    pool, store = await _seeded_store(tmp_path, 4)

    db_path = str(tmp_path / "app.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (1, ?, 'up.txt', 'hashgrdup', 12, 'pending')",
        (str(tmp_path / "up.txt"),),
    )
    conn.commit()
    file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
    conn.close()
    (tmp_path / "up.txt").write_text("upload content", encoding="utf-8")

    processor = _processor(pool, store)
    patches = _scan_harness_patches(processor, tmp_path, "hashgrdup2")
    with ExitStack() as stack:
        for cm in patches:
            stack.enter_context(cm)
        with pytest.raises(EmbeddingDimensionChangedError):
            await processor.process_existing_file(
                file_id=file_id,
                file_path=str(tmp_path / "up.txt"),
                vault_id=1,
            )

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT status, error_message FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    conn.close()
    assert row is not None and row[0] == "error"
    assert row[1].startswith("DIMENSION_CHANGED:"), row[1]
    # The seeded file's vectors are intact.
    assert await store.count_by_file("1") == 1
