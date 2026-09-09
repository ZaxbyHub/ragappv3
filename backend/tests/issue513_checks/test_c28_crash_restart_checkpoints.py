"""C28 - AC28 (crash/restart checkpoints): a processor killed mid-pipeline must
be completed by restart recovery without duplicate vector publication and
without a stranded processing row.

Contract under test (issue #513 AC28):

  Simulated crash state (no real process killing): a file row sits at
  status='processing' / phase='embedding' with a RECENT phase_started_at, and
  the vector store already holds ONE of the document's two chunk rows (a
  partial write that completed before the crash). A new processor is then
  started on the same DB + vector store (the restart). Restart recovery must:

    * complete the document (status leaves 'processing' and reaches a
      successful indexed state),
    * leave no stranded processing row,
    * publish no duplicate vectors (the file ends with EXACTLY its two chunk
      rows in the vector store - the pre-crash row must not be duplicated).

  The mid-embedding stage is chosen deliberately: HEAD's startup recovery only
  re-enqueues pending/phase='queued' rows and processing rows older than 30
  minutes, so a recent mid-pipeline crash is NOT recovered today (the sweep
  comment says such rows are left "for the operator to investigate").

Pre-fix expectation: after restart the row is still status='processing'
(stranded) -> FAIL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import sys
import tempfile
import time
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

_WAIT_S = 10.0
_DIM = 4
_FILE_HASH = "hashc28crash"


class _FixedEmbedding:
    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [[0.25 * (i + 1) for i in range(_DIM)] for _ in texts], []


def _record(rec_id: str, file_id: int, text: str) -> dict:
    return {
        "id": rec_id,
        "text": text,
        "file_id": str(file_id),
        "vault_id": "1",
        "chunk_index": 0,
        "metadata": "{}",
        "embedding": [0.25 * (i + 1) for i in range(_DIM)],
    }


def _file_status(db_path: str, file_id: int) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT status FROM files WHERE id = ?", (file_id,)).fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


async def _scenario() -> str:
    import app.services.background_tasks as bt
    from app.config import settings
    from app.models.database import get_pool, init_db
    from app.services.vector_store import VectorStore

    tmp = Path(tempfile.mkdtemp(prefix="c28_db_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    upload = tmp / "doc.txt"
    upload.write_text("crash test document body", encoding="utf-8")
    # The simulated mid-embedding crash state: recent phase_started_at (the
    # honest checkpoint state of a process that died seconds ago).
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status, "
        "phase, phase_started_at) VALUES (?, ?, ?, ?, 8, 'processing', 'embedding', "
        "datetime('now'))",
        (vault_id, str(upload), "doc.txt", _FILE_HASH),
    )
    conn.commit()
    file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
    conn.close()

    pool = get_pool(db_path, max_size=3)
    store = VectorStore(db_path=tmp / "lancedb")

    orig_instance = bt._processor_instance
    bt._processor_instance = None
    processor = bt.BackgroundProcessor(
        max_retries=1, retry_delay=0.01, pool=pool,
        vector_store=store, embedding_service=_FixedEmbedding(),
    )
    try:
        with patch.object(settings, "data_dir", tmp), \
                patch.object(settings, "wiki_enabled", False), \
                patch.object(settings, "wiki_compile_on_ingest", False), \
                patch.object(settings, "kms_enabled", False), \
                patch.object(settings, "multi_scale_indexing_enabled", False), \
                patch.object(settings, "contextual_chunking_enabled", False), \
                patch.object(settings, "optimize_mode", "manual"), \
                patch(
                    "app.services.document_processor.compute_file_hash",
                    return_value=_FILE_HASH,
                ), \
                patch.object(
                    processor.processor,
                    "_process_document_file",
                    new=AsyncMock(return_value=(_crash_chunks(), "crash test document body", _parsed())),
                ), \
                patch("app.services.document_processor.compute_parent_windows"):

            # Pre-crash partial vector write: chunk 0 is already durable.
            await store.init_table(_DIM)
            await store.add_chunks([_record(f"{file_id}_0", file_id, "chapter 0 text")])
            pre_rows = await store.count_by_file(str(file_id))
            if pre_rows != 1:
                return f"harness invalid: pre-crash seed wrote {pre_rows} rows"

            # ---- The restart ----
            await asyncio.wait_for(processor.start(), timeout=6.0)
            deadline = time.monotonic() + _WAIT_S
            status = _file_status(db_path, file_id)
            while time.monotonic() < deadline:
                status = _file_status(db_path, file_id)
                if status not in ("processing", "pending"):
                    break
                await asyncio.sleep(0.2)
            # give any in-flight worker a moment to finish vector writes
            await asyncio.sleep(0.5)

            if status == "processing":
                return (
                    "restart left the mid-embedding crash stranded: files row still "
                    "status='processing' (phase='embedding', recent phase_started_at) "
                    "- no stage checkpoint recovery for a mid-pipeline crash (AC28)"
                )
            if status != "indexed":
                return (
                    f"restart did not complete the crashed document: status='{status}'"
                )
            final_rows = await store.count_by_file(str(file_id))
            if final_rows != 2:
                return (
                    f"duplicate/missing vector publication after restart recovery: "
                    f"expected exactly 2 chunk rows for file_id={file_id}, found "
                    f"{final_rows} (AC28)"
                )
        return ""
    finally:
        processor.shutdown_event.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(processor.stop(timeout=1.0), timeout=5.0)
        processor._running = False
        bt._processor_instance = orig_instance


def _crash_chunks():
    from app.services.chunking import ProcessedChunk

    return [
        ProcessedChunk(
            text=f"chapter {i} text",
            metadata={"chunk_scale": "default", "raw_text": f"chapter {i}"},
            chunk_index=i,
        )
        for i in range(2)
    ]


def _parsed():
    from app.services.document_artifacts import ParsedDocument

    return ParsedDocument(atoms=())


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    reason = asyncio.run(_scenario())
    if reason:
        print(f"C28 CHECK: FAIL: {reason}")
        return 1
    print("C28 CHECK: PASS")
    return 0


def test_c28_crash_restart_checkpoints() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
