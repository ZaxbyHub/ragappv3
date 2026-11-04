"""Success-contract and stale-guard tests for the #1066 enrichment status flow.

Complements the frozen failure specs (tests/test_enrichment_false_success.py)
with the SUCCESS leg through the REAL ChunkEnrichmentService and REAL
DocumentProcessor.run_enrichment_job — the frozen C5 floor pins record
rendering only and cannot see a plumbing break that silently fails every real
generation (plan-critic round 1, finding 2) — plus the currency-guard leg for
a superseded failed attempt (plan-critic round 1, finding 4).

RED at base for the stale leg is not required: the guard is new-surface with
the #1066 fix; the success leg is GREEN at base and must stay GREEN.
"""

import asyncio
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.chunk_enrichment import ChunkEnrichmentService
from app.services.chunking import ProcessedChunk
from app.services.document_processor import DocumentProcessor


class FakeLLMClient:
    """Mirrors the real client contract: str return + finish_reason metrics.

    finish_reason lands on ``last_metrics`` (llm_client.py success path) and
    on the optional per-call ``metrics_sink`` kwarg, so the fake stays faithful
    whichever way the service reads it.
    """

    def __init__(self, content, finish_reason="stop"):
        self.content = content
        self.finish_reason = finish_reason
        self.calls = 0

    async def chat_completion(self, messages, **kwargs):
        self.calls += 1
        metrics = {
            "provider_url": "fake",
            "model": "fake",
            "finish_reason": self.finish_reason,
            "status": "ok",
        }
        self.last_metrics = dict(metrics)
        sink = kwargs.get("metrics_sink")
        if isinstance(sink, dict):
            sink.clear()
            sink.update(metrics)
        return self.content


class _EnrichmentJobHarness:
    """Real DocumentProcessor + temp sqlite DB wired for run_enrichment_job."""

    def __init__(self, file_id, file_hash="abcdef012345", chunks=None):
        self.temp_dir = tempfile.mkdtemp(prefix="rag-enr-contract-")
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'Default', '')"
        )
        conn.execute(
            """
            INSERT INTO files
                (id, vault_id, file_path, file_name, file_hash, file_size, status, chunk_count)
            VALUES (?, 1, ?, 't.sql', ?, 1, 'indexed', 1)
            """,
            (file_id, os.path.join(self.temp_dir, "t.sql"), file_hash),
        )
        conn.commit()
        conn.close()
        self.file_id = file_id
        self.file_hash = file_hash
        self.sql_file_path = os.path.join(self.temp_dir, "t.sql")
        with open(self.sql_file_path, "w", encoding="utf-8") as f:
            f.write("CREATE TABLE t (id INTEGER);")
        self._original_data_dir = settings.data_dir
        self._original_enabled = settings.chunk_enrichment_enabled
        settings.data_dir = Path(self.temp_dir)
        settings.chunk_enrichment_enabled = True
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.processor = DocumentProcessor(
            chunk_size_chars=2000, chunk_overlap_chars=200, pool=self.pool
        )
        self.swapped_records: list = []
        vector_store = MagicMock()
        vector_store.add_chunks_then_delete_ids = AsyncMock(
            side_effect=self._capture_swap
        )
        # _verify_vector_rows_visible awaits count_by_file after the swap.
        vector_store.count_by_file = AsyncMock(side_effect=lambda file_id: 1)
        self.processor.vector_store = vector_store
        emb = MagicMock()
        emb.MAX_TEXT_LENGTH = 8192
        emb.embedding_doc_prefix = ""
        emb.embedding_model = "contract-embed-model"
        emb.provider_mode = "probe"
        emb.embed_batch = AsyncMock(
            side_effect=lambda texts, fail_fast=False: (
                [[0.1, 0.2, 0.3, 0.4] for _ in texts],
                [],
            )
        )
        self.processor.embedding_service = emb
        self.chunks = chunks or [
            ProcessedChunk(
                text="contract leg text long enough to enrich",
                metadata={"chunk_scale": "default", "total_chunks": 1},
                chunk_index=0,
            )
        ]

    async def _capture_swap(self, records, old_ids):
        self.swapped_records.extend(records)
        return len(records)

    def attach_client(self, fake_client):
        self.processor._llm_client = fake_client
        self.processor._chunk_enrichment_service = ChunkEnrichmentService(
            llm_client=fake_client, concurrency=2
        )

    def run(self):
        asyncio.run(
            self.processor.run_enrichment_job(
                file_id=self.file_id,
                file_path=self.sql_file_path,
                vault_id=1,
                file_hash=self.file_hash,
                chunks=self.chunks,
                document_text="contract leg text long enough to enrich",
            )
        )

    def row(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, enrichment_status, enrichment_error FROM files WHERE id=?",
            (self.file_id,),
        ).fetchone()
        conn.close()
        return dict(row)

    def close(self):
        settings.data_dir = self._original_data_dir
        settings.chunk_enrichment_enabled = self._original_enabled
        self.pool.close_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TestEnrichmentSuccessContract(unittest.TestCase):
    def test_closed_json_with_stop_finish_stores_complete_and_summary(self):
        """Real service + real job: valid closed JSON, finish stop → complete."""
        h = _EnrichmentJobHarness(file_id=301)
        fake = FakeLLMClient(
            '{"summary": "a trustworthy summary", "questions": [], "entities": [], "aliases": []}',
            finish_reason="stop",
        )
        h.attach_client(fake)
        try:
            h.run()
            row = h.row()
        finally:
            h.close()

        self.assertEqual(fake.calls, 1, "the model must actually be called")
        self.assertEqual(row["enrichment_status"], "complete")
        self.assertIsNone(row["enrichment_error"])
        self.assertEqual(row["status"], "indexed")
        self.assertEqual(len(h.swapped_records), 1, "the enriched record must be swapped in")
        swapped = h.swapped_records[0]
        self.assertIn("a trustworthy summary", swapped["text"])
        self.assertIn("contract leg text long enough to enrich", swapped["text"])

    def test_superseded_failed_attempt_marks_stale_without_clobbering(self):
        """A failed candidate on a superseded attempt must not overwrite a newer status.

        Currency-guard leg for the #1066 failure write: the file hash changes
        while the (failing) generation is in flight; the attempt may only mark
        itself stale — never write a terminal error over the newer job's row.
        """
        h = _EnrichmentJobHarness(file_id=302, file_hash="oldhash012345")
        fake = FakeLLMClient('{"summary": "trunc', finish_reason="stop")
        h.attach_client(fake)

        original_is_current = DocumentProcessor._is_enrichment_job_current
        original_mark_stale = DocumentProcessor._mark_enrichment_stale_if_current_job

        is_current_calls: list = []

        def fake_is_current(self, file_id, file_hash):
            # Sync by contract. The first call is the job-entry currency check
            # (must pass so the attempt actually runs); the second is the
            # failure-block guard, which must see the attempt as superseded.
            is_current_calls.append(file_id)
            return len(is_current_calls) == 1

        stale_calls: list = []

        async def fake_mark_stale(self, file_id, processing_started_at):
            stale_calls.append((file_id, processing_started_at))

        try:
            DocumentProcessor._is_enrichment_job_current = fake_is_current
            DocumentProcessor._mark_enrichment_stale_if_current_job = fake_mark_stale
            h.run()
            row = h.row()
        finally:
            DocumentProcessor._is_enrichment_job_current = original_is_current
            DocumentProcessor._mark_enrichment_stale_if_current_job = original_mark_stale
            h.close()

        self.assertEqual(len(stale_calls), 1, "the superseded attempt must mark stale once")
        self.assertEqual(
            len(is_current_calls), 2,
            "entry check passes, failure-block guard sees superseded",
        )
        # No terminal error written by the superseded attempt: the row keeps
        # whatever the newer job set (here 'processing', the pre-run state).
        self.assertNotEqual(row["enrichment_status"], "complete")
        self.assertEqual(h.swapped_records, [], "a failed/superseded attempt must not swap")


if __name__ == "__main__":
    unittest.main()
