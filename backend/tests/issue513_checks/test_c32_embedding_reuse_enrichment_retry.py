"""C32 - AC32 (UPLOAD-DEEP-03): enrichment-only retries over unchanged chunk
text must REUSE embeddings instead of re-embedding.

Contract under test (issue #513 AC32):

  ``DocumentProcessor.run_enrichment_job`` is run twice for the same indexed
  file with IDENTICAL chunks, identical enrichment outputs, and the same
  embedding model/prefix/dimension contract (an enrichment-only retry):

  * FIRST run: the enriched texts are embedded (2 texts) and the enriched
    vector records are swapped in - unchanged baseline behavior;
  * SECOND run (the retry): ZERO unchanged texts may be re-embedded - the
    embeddings must be reused and only metadata updated (the enriched records
    are still (re)written to the vector store, but without new embedding work).

Pre-fix expectation: every enrichment run calls ``embed_batch`` over ALL
chunk texts (document_processor.py ~1498: ``embed_batch(enrichment_texts)``),
so the retry re-embeds both unchanged texts -> FAIL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c32_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR


class _CountingEmbedding:
    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim
        self.calls: list[list[str]] = []

    @property
    def embedded_texts(self) -> list[str]:
        return [t for call in self.calls for t in call]

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        self.calls.append(list(texts))
        return [[0.25 * (i + 1) for i in range(self._dim)] for _ in texts], []


class _CapturingVectorStore:
    def __init__(self) -> None:
        self.writes: list[dict] = []

    async def add_chunks_then_delete_ids(self, records, old_ids):  # noqa: ANN001, ANN202
        self.writes.extend(records)
        return 0

    async def init_table(self, embedding_dim: int) -> None:  # noqa: ANN001
        return None

    async def count_by_file(self, file_id) -> int:  # noqa: ANN001
        return len(self.writes)


class _StaticEnrichmentService:
    """Deterministic enrichment double: same outputs for the same chunks."""

    def __init__(self) -> None:
        self.calls = 0

    async def enrich_chunks(self, chunk_dicts, document_title):  # noqa: ANN001, ANN202
        from app.services.chunk_enrichment import ChunkEnrichment

        self.calls += 1
        return [
            ChunkEnrichment(
                chunk_id=d["chunk_uid"],
                summary="summary for " + d["chunk_uid"],
                questions=["q1"],
                entities=["e1"],
                aliases=[],
            )
            for d in chunk_dicts
        ]


def _chunks():
    from app.services.chunking import ProcessedChunk

    return [
        ProcessedChunk(
            text=f"stable chunk text number {i} for the reuse contract",
            metadata={"chunk_scale": "default", "raw_text": f"stable chunk {i}"},
            chunk_index=i,
        )
        for i in range(2)
    ]


async def _scenario() -> str:
    import sqlite3

    from app.config import settings
    from app.models.database import get_pool, init_db
    from app.services.document_processor import DocumentProcessor

    tmp = Path(tempfile.mkdtemp(prefix="c32_db_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    upload = tmp / "doc.txt"
    upload.write_text("enrichment reuse document", encoding="utf-8")
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status, "
        "enrichment_enabled) VALUES (?, ?, ?, 'hc32', 8, 'indexed', 1)",
        (vault_id, str(upload), "doc.txt"),
    )
    conn.commit()
    file_id = conn.execute("SELECT id FROM files WHERE file_hash = 'hc32'").fetchone()[0]
    conn.close()

    embedder = _CountingEmbedding()
    vstore = _CapturingVectorStore()
    processor = DocumentProcessor(
        pool=get_pool(db_path, max_size=3),
        embedding_service=embedder,
        vector_store=vstore,
        llm_client=object(),
    )
    processor._chunk_enrichment_service = _StaticEnrichmentService()

    chunks = _chunks()
    document_text = " ".join(c.text for c in chunks)
    kwargs = dict(
        file_id=file_id,
        file_path=str(upload),
        vault_id=vault_id,
        file_hash="hc32",
        chunks=chunks,
        document_text=document_text,
    )

    with patch.object(settings, "data_dir", tmp), \
            patch.object(settings, "multi_scale_indexing_enabled", False):
        run1_embedded = len(embedder.embedded_texts)
        await processor.run_enrichment_job(**kwargs)
        run1_total = len(embedder.embedded_texts)
        run1_writes = len(vstore.writes)
        if run1_total - run1_embedded != 2 or run1_writes < 2:
            return (
                "harness invalid: first enrichment run did not embed the 2 chunk "
                f"texts and write enriched records (embedded={run1_total - run1_embedded}, "
                f"writes={run1_writes})"
            )

        # The enrichment-only retry over byte-identical text.
        before = len(embedder.embedded_texts)
        writes_before = len(vstore.writes)
        await processor.run_enrichment_job(**kwargs)
        reembedded = len(embedder.embedded_texts) - before
        new_writes = len(vstore.writes) - writes_before

        if reembedded != 0:
            return (
                f"enrichment retry re-embedded {reembedded} unchanged chunk text(s) "
                f"under the same model contract - embeddings must be reused with "
                f"metadata-only updates (AC32)"
            )
        if new_writes < 2:
            return (
                f"enrichment retry performed no metadata/enriched-record update "
                f"({new_writes} record writes) - reuse must still publish the "
                f"enriched records (AC32)"
            )
    return ""


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    reason = asyncio.run(_scenario())
    if reason:
        print(f"C32 CHECK: FAIL: {reason}")
        return 1
    print("C32 CHECK: PASS")
    return 0


def test_c32_embedding_reuse_enrichment_retry() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
