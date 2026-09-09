"""C27 - AC27 (status model): partial-failure ingestion must surface a truthful
partial/searchable state distinct from full success and terminal failure, and
a historical error message must not persist on a successful current status.

Contract under test (issue #513 AC27):

  Clause 1 - a document where SOME chunks fail embedding (below the >50%
  abort threshold) must land in a status/model state that is DISTINCT from the
  fully-indexed status AND from terminal failure, while the successfully
  embedded chunks remain searchable (partial content is retrievable).

  Clause 2 - retry-to-success must clear attempt-scoped error text: after a
  failed ingest attempt that wrote files.error_message, a subsequent
  successful ingest of the same file must leave error_message NULL/empty.

Pre-fix expectation: files.status collapses to 'indexed' for the partial case
(same value as full success; only chunks_failed differs) and
``_update_status(..., 'indexed')`` never clears error_message, so a stale
error persists on the successful status -> FAIL (clause 1 reported first).
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
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c27_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR


class _ProgrammableEmbedding:
    """Embedding double that fails exactly the configured chunk indices."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim
        self.fail_indices: set[int] = set()

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        out = []
        for i, _t in enumerate(texts):
            if i in self.fail_indices:
                out.append(None)
            else:
                out.append([0.25 * (j + 1) for j in range(self._dim)])
        # batch_size is patched to 1, so batch index == chunk index
        return out, sorted(self.fail_indices)


class _CapturingVectorStore:
    def __init__(self) -> None:
        self.inserted: list[dict] = []

    async def init_table(self, embedding_dim: int) -> None:  # noqa: ANN001
        return None

    async def add_chunks(self, records):  # noqa: ANN001, ANN202
        self.inserted.extend(records)
        return {"vector_write_ms": 0.0, "optimize_ms": 0.0}

    async def delete_old_generation_by_file(self, file_id, gen_prefix):  # noqa: ANN001, ANN202
        return 0

    async def delete_by_file(self, file_id):  # noqa: ANN001, ANN202
        return 0

    async def count_by_file(self, file_id) -> int:  # noqa: ANN001
        return self.sync_count_by_file(file_id)

    def sync_count_by_file(self, file_id) -> int:  # noqa: ANN001
        return sum(1 for r in self.inserted if r.get("file_id") == str(file_id))


def _chunks():
    from app.services.chunking import ProcessedChunk

    return [
        ProcessedChunk(
            text=f"chapter {i} body text with plenty of words to embed {i}",
            metadata={"chunk_scale": "default", "raw_text": f"chapter {i}"},
            chunk_index=i,
        )
        for i in range(3)
    ]


def _file_row(db_path, name: str) -> int:
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (1, ?, ?, ?, 8, 'pending')",
        (f"/tmp/{name}.txt", name, f"hash-{name}"),
    )
    conn.commit()
    fid = conn.execute("SELECT id FROM files WHERE file_hash = ?", (f"hash-{name}",)).fetchone()[0]
    conn.close()
    return fid


def _row(db_path: str, file_id: int) -> dict:
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, error_message, chunks_failed, chunk_count FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _partial_marker_column(db_path: str) -> str | None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
    finally:
        conn.close()
    return next((c for c in cols if "partial" in c.lower()), None)


async def _ingest(processor, file_id: int, tmp: Path, name: str) -> None:  # noqa: ANN202
    from app.services.document_artifacts import ParsedDocument

    chunks = _chunks()
    with patch.object(
        processor,
        "_process_document_file",
        new=AsyncMock(return_value=(chunks, " ".join(c.text for c in chunks), ParsedDocument(atoms=()))),
    ), patch(
        "app.services.document_processor.compute_file_hash", return_value=f"hash-{name}"
    ), patch(
        "app.services.document_processor.compute_parent_windows"
    ):
        await processor.process_existing_file(
            file_id=file_id, file_path=str(tmp / f"{name}.txt"), vault_id=1
        )


async def _scenario() -> str:
    import sqlite3

    from app.config import settings
    from app.models.database import get_pool, init_db
    from app.services.document_processor import DocumentProcessor

    tmp = Path(tempfile.mkdtemp(prefix="c27_db_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    conn.commit()
    conn.close()
    (tmp / "partial.txt").write_text("partial doc", encoding="utf-8")
    (tmp / "retry.txt").write_text("retry doc", encoding="utf-8")

    partial_id = _file_row(db_path, "partial")
    retry_id = _file_row(db_path, "retry")

    embedder = _ProgrammableEmbedding()
    vstore = _CapturingVectorStore()
    processor = DocumentProcessor(
        pool=get_pool(db_path, max_size=3),
        embedding_service=embedder,
        vector_store=vstore,
    )

    with patch.object(settings, "data_dir", tmp), \
            patch.object(settings, "wiki_enabled", False), \
            patch.object(settings, "wiki_compile_on_ingest", False), \
            patch.object(settings, "kms_enabled", False), \
            patch.object(settings, "multi_scale_indexing_enabled", False), \
            patch.object(settings, "contextual_chunking_enabled", False), \
            patch.object(settings, "embedding_batch_size", 1):

        # --- Clause 1: partial embedding failure (1 of 3 chunks, 33%) ---
        embedder.fail_indices = {1}
        try:
            await _ingest(processor, partial_id, tmp, "partial")
        except Exception as exc:  # noqa: BLE001
            return f"harness invalid: partial ingest crashed: {exc!r}"
        prow = _row(db_path, partial_id)
        marker_col = _partial_marker_column(db_path)
        marker_set = bool(marker_col) and bool(prow.get(marker_col))
        partial_truthful = (prow.get("status") not in ("indexed", "error")) or (
            prow.get("status") == "indexed" and marker_set
        )
        searchable = vstore.sync_count_by_file(str(partial_id)) == 2
        if not partial_truthful:
            return (
                f"partial-failure ingestion collapsed to status='{prow.get('status')}' "
                f"(chunks_failed={prow.get('chunks_failed')}, chunk_count="
                f"{prow.get('chunk_count')}, partial-marker column={marker_col!r}) - "
                f"indistinguishable from full success / terminal failure (AC27)"
            )
        if not searchable:
            return (
                "partial-failure ingestion did not keep the successfully embedded "
                f"chunks searchable ({vstore.sync_count_by_file(str(partial_id))} rows)"
            )

        # --- Clause 2: retry-to-success clears the historical error ---
        embedder.fail_indices = {0, 1}
        try:
            await _ingest(processor, retry_id, tmp, "retry")
        except Exception:  # noqa: BLE001
            pass  # expected: >50% failure aborts the ingest with an error status
        rrow = _row(db_path, retry_id)
        if rrow.get("status") != "error" or not rrow.get("error_message"):
            return (
                "harness invalid: the failed first attempt did not produce "
                f"status='error' with an error message (got {rrow})"
            )
        embedder.fail_indices = set()
        try:
            await _ingest(processor, retry_id, tmp, "retry")
        except Exception as exc:  # noqa: BLE001
            return f"harness invalid: retry-to-success crashed: {exc!r}"
        rrow = _row(db_path, retry_id)
        if rrow.get("status") == "indexed" and rrow.get("error_message"):
            return (
                f"historical error message persists on the successful status: "
                f"status='{rrow.get('status')}', error_message={rrow.get('error_message')!r} "
                f"(AC27)"
            )
        if rrow.get("status") != "indexed":
            return (
                f"harness invalid: retry-to-success ended at status={rrow.get('status')!r}"
            )
    return ""


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    reason = asyncio.run(_scenario())
    if reason:
        print(f"C27 CHECK: FAIL: {reason}")
        return 1
    print("C27 CHECK: PASS")
    return 0


def test_c27_status_model_partial() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
