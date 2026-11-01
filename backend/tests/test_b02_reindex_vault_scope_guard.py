"""B02 guard — vault-scoped reindex refuses the dimension migration.

Companion to the #691 bare-call fix (implementation-review finding 2): a
vault-scoped reindex job re-embeds only its own vault's files, but
``commit_dimension_rebuild`` swaps the GLOBAL ``chunks`` table. Before this
guard, ``_reindex_embed_all(…, vault_id=N)`` on a dimension change rebuilt
and committed a staged table holding only vault N's rows — every other
vault's indexed files lost their vectors while their rows still said
``status='indexed'`` (the same defect class as the bare-call wipe, at the
caller that was previously believed correct).

Contract pinned: a vault-scoped job on a dimension change fails FAST with an
actionable "run a full reindex" error, opens NO rebuild table, and never
re-embeds any file. A full (all-vaults) reindex keeps the whole-corpus
migration path (pinned end-to-end by C9 part 3).

Demonstrated RED on the pre-guard tree: without the guard the job opens the
rebuild (``begin_attempts == 1``), re-embeds vault 1's file, and completes —
leaving vault 2 lying indexed.
"""

import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor, ReindexOperatorGuidance

LIVE_DIM = 4
PROBE_DIM = 6


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


class _RecordingVectorStore:
    """Live table at the OLD dimension; records rebuild-open attempts."""

    def __init__(self):
        self.begin_attempts = 0

    async def get_live_embedding_dim(self):
        return LIVE_DIM

    async def begin_dimension_rebuild(self, new_dim):  # pragma: no cover
        self.begin_attempts += 1
        raise AssertionError(
            f"vault-scoped job opened a dimension rebuild (new_dim={new_dim})"
        )


class ReindexVaultScopeGuardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(mkdtemp(prefix="b02-vaultscope-"))
        # Registered FIRST so (LIFO) it runs LAST, after the connection and
        # pool have been closed and the sqlite/lancedb files are unlockable.
        self.addCleanup(shutil.rmtree, tmp, True)
        self.db_path = tmp / "app.db"
        run_migrations(str(self.db_path))
        self.conn = _connect(str(self.db_path))
        self.addCleanup(self.conn.close)
        self.pool = SQLiteConnectionPool(str(self.db_path), max_size=4)
        self.addCleanup(self.pool.close_all)
        self.processor = BackgroundProcessor(pool=self.pool, retry_delay=0.05)
        self.emb = _ContractFaithfulEmbeddingService()
        self.store = _RecordingVectorStore()
        # _reindex_embed_all reads the wrapped DocumentProcessor's services.
        self.processor.processor.embedding_service = self.emb
        self.processor.processor.vector_store = self.store

    def _insert_indexed_file(self, vault_id, file_path):
        self.conn.execute(
            "INSERT INTO vaults (id, name) VALUES (?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (vault_id, f"vault-{vault_id}"),
        )
        self.conn.execute(
            "INSERT INTO files (file_name, file_path, vault_id, status, file_size) "
            "VALUES ('fixture.txt', ?, ?, 'indexed', 32)",
            (file_path, vault_id),
        )
        self.conn.commit()

    async def test_vault_scoped_reindex_refuses_dimension_migration(self):
        """A vault-scoped dimension-changing reindex fails fast and opens nothing.

        Two vaults each hold an indexed file; the job is scoped to vault 1
        while the live index sits at the OLD dimension and the embedding
        service produces the NEW one. The job must fail with the actionable
        full-reindex guidance BEFORE opening a rebuild or re-embedding any
        file — otherwise vault 2's vectors would be wiped by the global swap.
        """
        self._insert_indexed_file(1, "does/not/exist-v1.txt")
        self._insert_indexed_file(2, "does/not/exist-v2.txt")

        status, result, error = await self.processor._reindex_embed_all(  # noqa: SLF001
            1, vault_id=1
        )

        self.assertEqual(status, "failed")
        self.assertIsNotNone(error)
        # issue #702: the guidance travels as the verbatim operator-guidance
        # exception object (persisted verbatim at the lease settle boundary).
        self.assertIsInstance(error, ReindexOperatorGuidance)
        self.assertIn("vault-scoped reindex cannot migrate", str(error))
        self.assertIn("full reindex", str(error))
        # The rebuild was never opened: nothing can commit a partial corpus.
        self.assertEqual(self.store.begin_attempts, 0)
        # Only the probe ran; no file was re-embedded.
        self.assertEqual(self.emb.calls, [(("dimension_probe",), True)])
        self.assertEqual(result, {})
