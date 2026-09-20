"""Issue #229 F3 live-qualification regression: the reindex dimension probe.

The #513 W13 reindex dimension probe called
``embed_batch(["dimension_probe"], fail_fast=True)`` but tuple-unpacked the
result as ``(embeddings, failed)`` — the return contract of the
``fail_fast=False`` mode. With a healthy embedding service (the only case the
live qualification hit), ``fail_fast=True`` returns a bare list of vectors, so
every reindex job died with ``ValueError: not enough values to unpack
(expected 2, got 1)`` after the bounded retries (observed live on the R640AI
deployment, job 392 at build edd2c741: ``attempt_cap_exceeded``).

The probe ran untested because the existing reindex fixtures (#559 lease
suite) exercise empty file sets — the probe only runs when files exist — and
the #513 supplementary fake returned the tuple shape for BOTH modes.

Discriminating check: with a contract-faithful embedding service (list for
fail_fast=True, tuple for fail_fast=False) and one indexed file present, the
probe must complete and the job body must RETURN a status tuple instead of
raising. Reverting the fix reintroduces the ValueError.
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor

PROBE_DIM = 4


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class _ContractFaithfulEmbeddingService:
    """Mirrors EmbeddingService.embed_batch's real return contract."""

    def __init__(self):
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    async def embed_batch(self, texts, batch_size=None, fail_fast=True):
        self.calls.append((tuple(texts), bool(fail_fast)))
        vectors = [[float(i)] * PROBE_DIM for i, _ in enumerate(texts)]
        if fail_fast:
            return vectors
        return vectors, []


class _StubVectorStore:
    async def get_live_embedding_dim(self):
        return PROBE_DIM


class ReindexDimensionProbeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(mkdtemp(prefix="issue229-probe-"))
        self.db_path = tmp / "app.db"
        run_migrations(str(self.db_path))
        self.conn = _connect(str(self.db_path))
        self.addCleanup(self.conn.close)
        self.pool = SQLiteConnectionPool(str(self.db_path), max_size=4)
        self.addCleanup(self.pool.close_all)
        self.processor = BackgroundProcessor(pool=self.pool, retry_delay=0.05)
        self.emb = _ContractFaithfulEmbeddingService()
        # _reindex_embed_all reads the wrapped DocumentProcessor's services.
        self.processor.processor.embedding_service = self.emb
        self.processor.processor.vector_store = _StubVectorStore()

    def _insert_indexed_file(self, file_path):
        self.conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, file_size) "
            "VALUES ('probe_fixture.txt', ?, 1, 'indexed', 32)",
            (file_path,),
        )
        self.conn.commit()

    async def test_probe_survives_faithful_embed_batch_contract(self):
        """The probe completes against the real fail_fast=True return shape.

        Pre-fix this raises ValueError (tuple-unpack of a 1-element list);
        post-fix the job body returns a status tuple even though the
        (missing) fixture file itself fails per-file processing.
        """
        self._insert_indexed_file("does/not/exist.txt")

        status, result, error = await self.processor._reindex_embed_all(  # noqa: SLF001
            1, vault_id=1
        )

        self.assertEqual(status, "failed", "missing file must fail per-file, not crash the job")
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertIsNone(error)
        # The probe ran exactly once in fail_fast mode before any file work.
        self.assertEqual(len(self.emb.calls), 1)
        self.assertEqual(self.emb.calls[0], (("dimension_probe",), True))

    async def test_probe_skipped_without_files(self):
        """Zero files: the job completes without touching the embedding service."""
        status, result, error = await self.processor._reindex_embed_all(  # noqa: SLF001
            2, vault_id=1
        )
        self.assertEqual(status, "completed")
        self.assertEqual(result, {"processed": 0, "failed": 0})
        self.assertIsNone(error)
        self.assertEqual(self.emb.calls, [])
