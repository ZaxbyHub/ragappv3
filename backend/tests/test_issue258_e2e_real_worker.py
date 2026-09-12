"""Issue #258 (E2) acceptance checks — AC6 / TEST-008: real-worker e2e + real
row deletion.

Phase 2.5 CHECKS ONLY (tier L). TEST-008's defect: the "end-to-end"
``test_upload_index_chat_flow`` fully mocks DocumentProcessor and never chats
against indexed data (only lists documents); the deletion tests mock the
vector-store class and the "success" case never creates a row, never calls
DELETE, and the "not found" case is a bare ``pass``.

This file provides the two missing real-path nodes:

Node 1 — upload -> REAL in-process worker drain -> persisted 'indexed' ->
  chat retrieval of the stored chunk text.
  The route-side registration runs for real (duplicate check, files row,
  enqueue on the real BackgroundProcessor queue); the drain runs the REAL
  ``BackgroundProcessor._worker_loop`` in-process (the same loop body
  ``start()`` spawns); the REAL DocumentProcessor parses the uploaded .sql
  through the REAL in-repo SchemaParser (no unstructured dependency),
  chunks, embeds via the shared fake embedding service, and writes through a
  recording vector store that implements the real store call surface
  (add_chunks / count_by_file / delete_old_generation_by_file). The final
  status is read back from the SQLite files row, and the chat route is then
  driven against a real RAGEngine whose search returns the ACTUALLY STORED
  chunks — asserting the stored chunk text is retrievable.

Node 2 — real row + real on-disk file + stored vector records -> DELETE
  route -> row gone, vectors gone, second DELETE 404.
  Everything except the vector persistence backend is real: the route, the
  DB transaction, the derived-data purge, the files-row delete. The vector
  records live in the same recording store used by node 1 (a real LanceDB
  surface is the nightly-tier obligation, AC12).

Measured class at base a543361: both nodes expected PRESERVING-of-production
(green at base — production works; the gap was absent coverage). The
discriminators (worker-disabled mutant, delete no-op mutant) are Phase 4.5
mutation-probe territory.

NOTE: this module intentionally never names the token-verification
dependency; the shared conftest bypass applies (see testing docs section 2).
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from io import BytesIO

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from test_integration import (  # noqa: E402
    FakeEmbeddingService,
    FakeLLMClient,
    FakeMemoryStore,
    setup_app_state,
)

from app.services.background_tasks import BackgroundProcessor  # noqa: E402
from app.services.rag_engine import RAGEngine  # noqa: E402

_VAULT_ID = 1
_MARKER_TABLE = "issue258_sentinel_documents"
_MARKER_COLUMN = "issue258_retrieval_marker"
_SQL_CONTENT = (
    f"CREATE TABLE {_MARKER_TABLE} (\n"
    f"    id INTEGER PRIMARY KEY,\n"
    f"    {_MARKER_COLUMN} TEXT NOT NULL\n"
    ");\n"
).encode("utf-8")


class _FakeLanceDB:
    """Minimal double of the lancedb connection the delete purge inspects."""

    async def table_names(self):
        return ["chunks"]

    async def open_table(self, name):
        return object()


class WorkerEmbeddingService:
    """Embedding fake matching the REAL ``embed_batch`` call contract
    (``fail_fast=False`` returns ``(embeddings, failed_indices)`` — see
    document_processor's [N1] note); test_integration's FakeEmbeddingService
    is too thin for the real worker path."""

    def __init__(self):
        self.dim = 768
        self.batch_calls: list = []

    async def embed_single(self, text: str):
        return [0.1] * self.dim

    async def embed_batch(self, texts, batch_size=None, fail_fast=True):
        self.batch_calls.append((len(texts), batch_size, fail_fast))
        if fail_fast:
            return [[0.1] * self.dim for _ in texts]
        return ([[0.1] * self.dim for _ in texts], [])


class RecordingVectorStore:
    """Test double implementing the REAL VectorStore call surface used by
    the ingest path, the delete purge, and engine retrieval — while keeping
    the records inspectable."""

    def __init__(self):
        self.stored_chunks: list = []
        self.deleted_file_ids: list = []
        self.init_table_calls: list = []
        self.add_chunks_calls: list = []
        self.db = _FakeLanceDB()

    # -- ingest surface -------------------------------------------------
    async def init_table(self, dimension, **kwargs):
        self.init_table_calls.append((dimension, kwargs))
        return None

    async def add_chunks(self, records, **kwargs):
        self.add_chunks_calls.append(len(records))
        self.stored_chunks.extend(records)
        return None  # timings merge tolerates falsy

    async def delete_old_generation_by_file(self, file_id, generation, **kwargs):
        keep = [
            r
            for r in self.stored_chunks
            if not (
                r.get("file_id") == file_id
                and str(r.get("id", "")).endswith(generation)
            )
        ]
        removed = len(self.stored_chunks) - len(keep)
        self.stored_chunks = keep
        return removed

    async def delete_by_file(self, file_id, **kwargs):
        keep = [r for r in self.stored_chunks if r.get("file_id") != file_id]
        removed = len(self.stored_chunks) - len(keep)
        self.stored_chunks = keep
        self.deleted_file_ids.append(file_id)
        return removed

    async def count_by_file(self, file_id, **kwargs):
        return sum(1 for r in self.stored_chunks if r.get("file_id") == file_id)

    # -- retrieval surface ----------------------------------------------
    async def search(self, embedding, limit=10, filter_expr=None, vault_id=None, **kwargs):
        results = []
        for r in self.stored_chunks:
            raw_meta = r.get("metadata")
            if raw_meta is None:
                meta = {}
            elif isinstance(raw_meta, str):
                # Real records persist metadata as a JSON string; the real
                # store deserializes it on read — mirror that here.
                meta = json.loads(raw_meta)
            else:
                meta = dict(raw_meta)
            results.append(
                {
                    "text": r.get("text", ""),
                    "file_id": r.get("file_id", ""),
                    "metadata": meta,
                    "score": 0.9,
                }
            )
        return results[:limit]

    # -- lifecycle no-ops (engine/route compatibility) -------------------
    def connect(self):
        return None

    def close(self):
        return None

    def get_fts_exceptions(self):
        return 0

    async def get_chunks_by_uid(self, chunk_uids):
        return []


def _seed_vault(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (?, ?)",
            (_VAULT_ID, "issue258-e2e-vault"),
        )
        conn.commit()
    finally:
        conn.close()


def _files_row_status(db_path: str, file_id: int) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


async def _drain_queue(bp: BackgroundProcessor, timeout: float = 60.0) -> None:
    """Run the REAL worker loop until the ingestion queue is drained."""
    worker = asyncio.create_task(bp._worker_loop())
    try:
        await asyncio.wait_for(bp.queue.join(), timeout=timeout)
    finally:
        bp.shutdown_event.set()
        await asyncio.wait_for(worker, timeout=10.0)


class TestRealWorkerEndToEnd(unittest.TestCase):
    """AC6 node 1: upload -> real worker -> indexed -> retrievable."""

    def setUp(self):
        from app.config import settings

        self._orig_data_dir = settings.data_dir
        from app.main import app

        self.app = app
        self.store = RecordingVectorStore()
        self.embedding = WorkerEmbeddingService()
        self.llm = FakeLLMClient()
        self.memory = FakeMemoryStore()
        self._cleanup = setup_app_state(
            app,
            vector_store=self.store,
            embedding_service=self.embedding,
            llm_client=self.llm,
            memory_store=self.memory,
        )
        _seed_vault(app.state._test_db_path)

    def tearDown(self):
        from app.api.deps import get_rag_engine
        from app.config import settings

        self.app.dependency_overrides.pop(get_rag_engine, None)
        settings.data_dir = self._orig_data_dir
        self._cleanup()

    def test_upload_real_worker_indexed_then_retrievable(self):
        # A REAL BackgroundProcessor on the shared fakes + the real pool.
        bp = BackgroundProcessor(
            retry_delay=0,
            vector_store=self.store,
            embedding_service=self.embedding,
            pool=self.app.state.db_pool,
            llm_client=self.llm,
            maintenance_service=self.app.state.maintenance_service,
        )
        self.app.state.background_processor = bp

        client = TestClient(self.app)
        response = client.post(
            "/api/documents/upload?vault_id=1",
            files={"file": ("issue258_e2e.sql", BytesIO(_SQL_CONTENT), "text/plain")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        upload = response.json()
        file_id = upload["file_id"]
        self.assertEqual(upload["status"], "pending")
        # The route enqueued onto the REAL processor's queue (not a mock).
        self.assertEqual(bp.queue.qsize(), 1)

        # Drain through the real worker loop, in-process.
        asyncio.run(_drain_queue(bp))

        db_path = self.app.state._test_db_path

        # AC6 CHECK — the real ingestion worker did not persist the uploaded
        # document to terminal 'indexed' status.
        print("AC6 CHECK: FAIL — real worker did not persist the uploaded document to 'indexed'")
        self.assertEqual(_files_row_status(db_path, file_id), "indexed")
        self.assertGreater(len(self.store.stored_chunks), 0)
        stored_text = " ".join(
            str(r.get("text", "")) for r in self.store.stored_chunks
        )
        self.assertIn(_MARKER_TABLE, stored_text)

        # Chat-route retrieval against the actually-stored chunks: a real
        # RAGEngine over the same fakes/store; its vector search returns what
        # the worker really wrote.
        engine = RAGEngine(
            embedding_service=self.embedding,
            vector_store=self.store,
            memory_store=self.memory,
            llm_client=self.llm,
        )
        from unittest.mock import patch

        from app.api.deps import get_rag_engine
        from app.config import settings as app_settings

        self.app.dependency_overrides[get_rag_engine] = lambda: engine
        with patch.object(app_settings, "context_distillation_enabled", False):
            chat = client.post(
                "/api/chat",
                json={
                    "message": "What tables are documented?",
                    "history": [],
                    "stream": False,
                },
            )
        self.assertEqual(chat.status_code, 200, chat.text)
        chat_data = chat.json()

        # AC6 CHECK — the stored chunk text is not retrievable through the
        # chat route (no sources cite the ingested marker table).
        print("AC6 CHECK: FAIL — stored chunk text not retrievable through the chat route")
        sources = chat_data.get("sources", [])
        self.assertGreater(len(sources), 0)

        def _source_text(s) -> str:
            # Serialized sources carry the chunk text under 'snippet'.
            return str(s.get("snippet") or s.get("text") or "")

        self.assertTrue(
            any(_MARKER_TABLE in _source_text(s) for s in sources),
            f"marker table absent from retrieved sources: {sources}",
        )
        self.assertTrue(
            any(str(s.get("file_id")) == str(file_id) for s in sources),
            f"retrieved sources do not cite the uploaded file_id {file_id}: {sources}",
        )


class TestDeleteRealRowAndVectors(unittest.TestCase):
    """AC6 node 2: real row/file/vectors -> DELETE -> gone + second 404."""

    def setUp(self):
        from app.config import settings

        self._orig_data_dir = settings.data_dir
        from app.main import app

        self.app = app
        self.store = RecordingVectorStore()
        self._cleanup = setup_app_state(app, vector_store=self.store)
        _seed_vault(app.state._test_db_path)

    def tearDown(self):
        from app.config import settings

        settings.data_dir = self._orig_data_dir
        self._cleanup()

    def _seed_real_row_and_vectors(self) -> int:
        """Create a REAL files row, a REAL on-disk file, and stored vectors."""
        db_path = self.app.state._test_db_path
        real_file = os.path.join(
            tempfile.mkdtemp(prefix="issue258_delete_"), "issue258_delete_target.bin"
        )
        with open(real_file, "wb") as fh:
            fh.write(b"issue258 real bytes awaiting deletion")

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.execute(
                """
                INSERT INTO files (vault_id, file_path, file_name, file_hash,
                                   file_size, file_type, status, source)
                VALUES (?, ?, ?, ?, ?, ?, 'indexed', 'upload')
                """,
                (
                    _VAULT_ID,
                    real_file,
                    "issue258_delete_target.bin",
                    "issue258hash0000000001",
                    37,
                    ".bin",
                ),
            )
            conn.commit()
            file_id = cursor.lastrowid
        finally:
            conn.close()

        asyncio.run(
            self.store.add_chunks(
                [
                    {
                        "id": f"{file_id}_issue258hash_default_0",
                        "file_id": str(file_id),
                        "text": "issue258 deletion fixture chunk 0",
                        "metadata": {"source_file": "issue258_delete_target.bin"},
                    },
                    {
                        "id": f"{file_id}_issue258hash_default_1",
                        "file_id": str(file_id),
                        "text": "issue258 deletion fixture chunk 1",
                        "metadata": {"source_file": "issue258_delete_target.bin"},
                    },
                ]
            )
        )
        self.assertEqual(asyncio.run(self.store.count_by_file(str(file_id))), 2)
        return file_id

    def test_delete_removes_row_and_vectors_then_second_404(self):
        file_id = self._seed_real_row_and_vectors()
        db_path = self.app.state._test_db_path

        client = TestClient(self.app)
        first = client.delete(f"/api/documents/{file_id}")
        # AC6 CHECK — the DELETE route did not succeed against a real
        # row/vector fixture (status or body contract broken).
        print("AC6 CHECK: FAIL — DELETE route did not succeed against the real row/vector fixture")
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual(body["file_id"], file_id)
        self.assertEqual(body["status"], "success")

        # AC6 CHECK — the files row survived the DELETE (a no-op delete
        # mutant would pass the 200 above but fail here).
        print("AC6 CHECK: FAIL — the files row survived the DELETE (no-op delete mutant)")
        self.assertIsNone(_files_row_status(db_path, file_id))

        # The vector purge ran against the store for THIS file.
        self.assertIn(str(file_id), self.store.deleted_file_ids)
        self.assertEqual(asyncio.run(self.store.count_by_file(str(file_id))), 0)

        # AC6 CHECK — a second DELETE of the same id must be 404.
        print("AC6 CHECK: FAIL — second DELETE of the same id must be 404")
        second = client.delete(f"/api/documents/{file_id}")
        self.assertEqual(second.status_code, 404)


if __name__ == "__main__":
    unittest.main()
