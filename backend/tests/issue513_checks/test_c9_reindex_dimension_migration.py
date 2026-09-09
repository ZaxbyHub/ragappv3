"""C9 - AC9 (INGEST-010): dimension-changing reindex must build a compatible
index and preserve the old one when the rebuild fails.

Contract under test (issue #513 AC9):

  Part 1 - a native LanceDB index seeded with the OLD embedding dimension is
  reindexed after the embedding dimension changed (the reindex path calls
  ``DocumentProcessor.process_existing_file`` exactly like
  ``BackgroundProcessor._process_reindex_job``): NEW-dimension vectors must end
  up searchable (table dim == new dim, rows retrievable for the file).

  Part 2 - when the rebuild FAILS mid-way (here: the write fails against the
  old-dimension table on the delete-first ordering used by
  ``reupload_safe_order=False``), the PRIOR old-dimension vectors must survive
  (still retrievable), not be destroyed.

Pre-fix expectation (RC-8): reindex mutates the live index in place -
``init_table`` reopens the old-dimension table, ``add_chunks`` raises
VectorStoreValidationError on the dimension mismatch (part 1: new vectors never
searchable) and on the delete-first ordering the old vectors are already gone
when the add fails (part 2) -> FAIL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""

OLD_DIM = 4
NEW_DIM = 6


class _FakeEmbeddingService:
    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [
            [((i + 1) * 0.25) for i in range(self._dim)] for _ in texts
        ], []


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


async def _table_dim(store) -> int:  # noqa: ANN001
    schema = await store.table.schema()
    field = schema.field("embedding")
    return int(field.type.list_size)


async def _reprocess_file(store, pool, db_vault_id: int, file_id: int, upload_path: Path, new_dim: int):  # noqa: ANN202
    """Drive the reindex step for one file exactly as _process_reindex_job does."""
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument
    from app.services.document_processor import DocumentProcessor

    chunk = ProcessedChunk(
        text="reindexed content with the new embedding model",
        metadata={"chunk_scale": "default", "raw_text": "reindexed content"},
        chunk_index=0,
    )
    processor = DocumentProcessor(
        pool=pool,
        embedding_service=_FakeEmbeddingService(dim=new_dim),
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
    ), patch(
        "app.services.document_processor.compute_file_hash", return_value="hashc9file"
    ), patch(
        "app.services.document_processor.compute_parent_windows"
    ):
        await processor.process_existing_file(
            file_id=file_id, file_path=str(upload_path), vault_id=db_vault_id
        )


async def _scenario() -> tuple[str, str]:
    """Returns (part1_reason, part2_reason); empty string means the part passed."""
    import sqlite3

    from app.config import settings
    from app.models.database import get_pool, init_db
    from app.services.vector_store import VectorStore, VectorStoreValidationError

    tmp = Path(tempfile.mkdtemp(prefix="c9_db_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, ?, ?, ?, ?, 'indexed')",
        (vault_id, str(tmp / "doc.txt"), "doc.txt", "hashc9file", 12),
    )
    conn.commit()
    file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
    conn.close()
    upload_path = tmp / "doc.txt"
    upload_path.write_text("seed content", encoding="utf-8")

    pool = get_pool(db_path, max_size=3)
    store = VectorStore(db_path=tmp / "lancedb")

    part1_reason = ""
    part2_reason = ""

    with patch.object(settings, "data_dir", tmp), \
            patch.object(settings, "wiki_enabled", False), \
            patch.object(settings, "wiki_compile_on_ingest", False), \
            patch.object(settings, "kms_enabled", False), \
            patch.object(settings, "multi_scale_indexing_enabled", False), \
            patch.object(settings, "contextual_chunking_enabled", False), \
            patch.object(settings, "optimize_mode", "manual"):

        # Seed the old-dimension native index.
        await store.init_table(OLD_DIM)
        await store.add_chunks([_record(f"{file_id}_0", file_id, "seed content", OLD_DIM)])
        seeded_count = await store.count_by_file(str(file_id))
        if seeded_count != 1:
            return f"harness invalid: seeded index has {seeded_count} rows", ""

        # ---- Part 1: dimension changed, reindex (safe-order default) ----
        dim_err: Exception | None = None
        try:
            await _reprocess_file(store, pool, vault_id, file_id, upload_path, NEW_DIM)
        except VectorStoreValidationError as exc:
            dim_err = exc
        except Exception as exc:  # noqa: BLE001
            dim_err = exc
        if dim_err is not None:
            part1_reason = (
                f"reindex with changed embedding dimension failed: {type(dim_err).__name__}"
                f"({dim_err}) - new-dimension vectors are not searchable (INGEST-010)"
            )
        else:
            dim = await _table_dim(store)
            count = await store.count_by_file(str(file_id))
            searchable = False
            if dim == NEW_DIM and count >= 1:
                try:
                    results = await store.search(
                        _record("q", file_id, "q", NEW_DIM)["embedding"], limit=5
                    )
                    searchable = any(r.get("file_id") == str(file_id) for r in results)
                except Exception:  # noqa: BLE001
                    searchable = False
            if not searchable:
                part1_reason = (
                    f"after dimension-changing reindex the store dim={dim}, "
                    f"rows={count}, new-dimension content searchable={searchable}"
                )

        # ---- Part 2: failed rebuild must preserve the old index ----
        tmp2 = Path(tempfile.mkdtemp(prefix="c9_db2_"))
        db_path2 = str(tmp2 / "app.db")
        init_db(db_path2)
        conn = sqlite3.connect(db_path2)
        conn.execute("INSERT INTO vaults (name) VALUES ('v2')")
        vault_id2 = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (?, ?, ?, ?, ?, 'indexed')",
            (vault_id2, str(tmp2 / "doc.txt"), "doc.txt", "hashc9file", 12),
        )
        conn.commit()
        file_id2 = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
        conn.close()
        (tmp2 / "doc.txt").write_text("seed content two", encoding="utf-8")

        pool2 = get_pool(db_path2, max_size=3)
        store2 = VectorStore(db_path=tmp2 / "lancedb")
        await store2.init_table(OLD_DIM)
        await store2.add_chunks([_record(f"{file_id2}_0", file_id2, "seed content two", OLD_DIM)])
        old_count = await store2.count_by_file(str(file_id2))
        if old_count != 1:
            return part1_reason, f"harness invalid: second seed has {old_count} rows"

        rebuild_failed = False
        with patch.object(settings, "reupload_safe_order", False):
            try:
                await _reprocess_file(store2, pool2, vault_id2, file_id2, tmp2 / "doc.txt", NEW_DIM)
            except Exception:  # noqa: BLE001
                rebuild_failed = True
        survivors = await store2.count_by_file(str(file_id2))
        if not rebuild_failed:
            part2_reason = (
                "harness invalid: the injected mid-rebuild failure did not fire "
                f"(old-dim rebuild unexpectedly succeeded; survivors={survivors})"
            )
        elif survivors < old_count:
            part2_reason = (
                f"failed rebuild destroyed the prior index: {old_count} -> {survivors} "
                f"old-dimension rows retrievable for file_id={file_id2} (INGEST-010)"
            )

    return part1_reason, part2_reason


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    part1_reason, part2_reason = asyncio.run(_scenario())
    if part1_reason:
        print(f"C9 CHECK: FAIL: {part1_reason}")
        return 1
    if part2_reason:
        print(f"C9 CHECK: FAIL: {part2_reason}")
        return 1
    print("C9 CHECK: PASS")
    return 0


def test_c9_reindex_dimension_migration() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
