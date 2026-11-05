"""Success-contract and currency-guard tests for the #1066 enrichment status flow.

Complements the frozen failure specs (tests/test_enrichment_false_success.py)
with the SUCCESS leg through the REAL ChunkEnrichmentService and REAL
DocumentProcessor.run_enrichment_job, the failure-block currency legs through
the REAL _is_enrichment_job_current / _mark_enrichment_stale_if_current_job
(review PRR-002: the earlier leg patched those methods out, so its oracle
could not see a terminal clobber), the mixed-batch no-swap pin (review
PRR-001), and the persisted-code pin (review PRR-013). All legs drive the
real service/job/store methods — no production-method patching.
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
from app.services.chunk_enrichment import (
    ChunkEnrichment,
    ChunkEnrichmentService,
)
from app.services.chunking import ProcessedChunk
from app.services.document_processor import DocumentProcessor
from app.services.llm_client import LLMError


class FakeLLMClient:
    """Mirrors the real client contract: str return + finish_reason metrics.

    finish_reason lands on ``last_metrics`` (llm_client.py success path) and
    on the optional per-call ``metrics_sink`` kwarg (replace semantics, like
    the real client), so the fake stays faithful whichever way the service
    reads it.
    """

    def __init__(self, content, finish_reason="stop", on_call=None):
        self.content = content
        self.finish_reason = finish_reason
        self.calls = 0
        self.last_metrics = {}
        self.on_call = on_call

    async def chat_completion(self, messages, **kwargs):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
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


class RaisedLLMClient:
    """Model call raises LLMError (the review PRR-002/PRR-013 failure arm)."""

    def __init__(self, on_call=None):
        self.calls = 0
        self.on_call = on_call

    async def chat_completion(self, messages, **kwargs):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        raise LLMError("provider down")


class MultiFakeLLMClient:
    """One behavior per candidate, in call order (review PRR-001 mixed batch)."""

    def __init__(self, behaviors):
        # behaviors: list of ("ok", content, finish_reason) or ("raise", msg)
        self._behaviors = list(behaviors)
        self.calls = 0

    async def chat_completion(self, messages, **kwargs):
        idx = self.calls
        self.calls += 1
        behavior = self._behaviors[idx]
        if behavior[0] == "raise":
            raise LLMError(behavior[1])
        _, content, finish_reason = behavior
        metrics = {
            "provider_url": "fake",
            "model": "fake",
            "finish_reason": finish_reason,
            "status": "ok",
        }
        sink = kwargs.get("metrics_sink")
        if isinstance(sink, dict):
            sink.clear()
            sink.update(metrics)
        return content


def _newer_job_mutation(db_path, file_id):
    """Simulate a newer job having re-ingested and written its own status
    mid-flight: hash rotated, enrichment columns moved to a newer attempt."""

    def mutate():
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            UPDATE files
            SET file_hash = 'newerhash654321',
                enrichment_status = 'processing',
                enrichment_error = 'newer job in flight',
                enrichment_updated_at = '2030-01-02T00:00:00+00:00'
            WHERE id = ?
            """,
            (file_id,),
        )
        conn.commit()
        conn.close()

    return mutate


def _hash_rotate_mutation(db_path, file_id):
    """Simulate a newer re-ingest registering a new hash BEFORE its own
    enrichment has touched the enrichment columns (they still hold this
    attempt's 'processing' write)."""

    def mutate():
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE files SET file_hash = 'newerhash654321' WHERE id = ?",
            (file_id,),
        )
        conn.commit()
        conn.close()

    return mutate


def _newer_terminal_write_then_raise(db_path, file_id):
    """Embed-side mutation for the ASK leg: a newer re-ingest (hash rotated)
    whose own enrichment has ALREADY reached its terminal 'complete' — then
    this attempt's embed fails. The except-handler's terminal write must not
    clobber the newer row."""

    async def failing_embed(texts, fail_fast=False):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            UPDATE files
            SET file_hash = 'newerhash654321',
                enrichment_status = 'complete',
                enrichment_error = NULL,
                enrichment_updated_at = '2030-01-02T00:00:00+00:00'
            WHERE id = ?
            """,
            (file_id,),
        )
        conn.commit()
        conn.close()
        raise RuntimeError("embed boom")

    return failing_embed


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


def _two_chunks():
    return [
        ProcessedChunk(
            text="first chunk with plenty of text to enrich",
            metadata={"chunk_scale": "default", "total_chunks": 2},
            chunk_index=0,
        ),
        ProcessedChunk(
            text="second chunk with plenty of text to enrich",
            metadata={"chunk_scale": "default", "total_chunks": 2},
            chunk_index=1,
        ),
    ]


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

    def test_mixed_batch_failure_never_swaps_successful_sibling(self):
        """Review PRR-001: one failed candidate must fail the file BEFORE the
        vector swap, so the successful sibling keeps its already-searchable
        base rows. A swap-then-error reorder flips this assertion (the frozen
        failure specs cannot observe the swap — their harness disables the
        vector store)."""
        h = _EnrichmentJobHarness(file_id=303, chunks=_two_chunks())
        fake = MultiFakeLLMClient(
            [
                ("ok", '{"summary": "good sibling summary"}', "stop"),
                ("raise", "provider down"),
            ]
        )
        h.attach_client(fake)
        try:
            h.run()
            row = h.row()
        finally:
            h.close()

        self.assertEqual(fake.calls, 2, "both candidates were generated")
        self.assertEqual(row["enrichment_status"], "error")
        self.assertIn("ENRICHMENT_FAILED", row["enrichment_error"] or "")
        self.assertEqual(
            h.swapped_records,
            [],
            "a failed batch must never swap — not even the successful sibling",
        )

    def test_newer_job_write_survives_a_superseded_failure(self):
        """Review PRR-002 (no-clobber, real methods): a newer attempt's row
        state must survive this attempt's failure. The mid-flight mutation
        rotates the hash (this attempt is genuinely superseded) and the newer
        job has already written its own enrichment columns, so the real
        _mark_enrichment_stale_if_current_job CAS must NOT match — no
        terminal error may land on the newer row."""
        h = _EnrichmentJobHarness(file_id=302, file_hash="abcdef012345")
        fake = RaisedLLMClient(on_call=_newer_job_mutation(h.db_path, 302))
        h.attach_client(fake)
        try:
            h.run()
            row = h.row()
        finally:
            h.close()

        self.assertEqual(fake.calls, 1, "the model was called once, then failed")
        self.assertEqual(
            row["enrichment_status"], "processing",
            "the newer attempt's status must survive untouched",
        )
        self.assertEqual(
            row["enrichment_error"], "newer job in flight",
            "the newer attempt's error text must survive untouched",
        )
        self.assertNotIn("ENRICHMENT_FAILED", row["enrichment_error"] or "")
        self.assertEqual(h.swapped_records, [])

    def test_stale_marker_write_is_distinguishable_from_terminal_error(self):
        """Review PRR-002 (stale-mark arm, real methods): when the attempt is
        superseded mid-flight and NO newer write has landed yet, the real
        stale-marker must fire with its OWN message — distinguishable from
        the ENRICHMENT_FAILED terminal write."""
        h = _EnrichmentJobHarness(file_id=304, file_hash="abcdef012345")
        fake = RaisedLLMClient(on_call=_hash_rotate_mutation(h.db_path, 304))
        h.attach_client(fake)
        try:
            h.run()
            row = h.row()
        finally:
            h.close()

        self.assertEqual(fake.calls, 1)
        self.assertEqual(row["enrichment_status"], "error")
        self.assertEqual(
            row["enrichment_error"],
            "Enrichment job became stale before completion; base index remains available",
            "the stale-marker message, not the ENRICHMENT_FAILED terminal write",
        )
        self.assertEqual(h.swapped_records, [])

    def test_failed_batch_persists_stable_code_with_count(self):
        """Review PRR-013: the failure block's persisted error is the stable
        #562 code + fixed reason plus the failed/total count — not a raw
        exception string."""
        h = _EnrichmentJobHarness(file_id=305)
        fake = RaisedLLMClient()
        h.attach_client(fake)
        try:
            h.run()
            row = h.row()
        finally:
            h.close()

        self.assertEqual(
            row["enrichment_error"],
            "ENRICHMENT_FAILED: content enrichment failed; the indexed document is unaffected (1 of 1 chunks)",
        )

    def test_enrichment_round_trip_preserves_error_signal(self):
        """Review PRR-010: to_dict → from_dict must round-trip the failure
        signal (the pre-fix from_dict dropped the new key)."""
        failed = ChunkEnrichment(
            chunk_id="c1",
            error="enrichment JSON failed to parse",
        )
        restored = ChunkEnrichment.from_dict(failed.to_dict())
        self.assertEqual(restored.error, "enrichment JSON failed to parse")
        ok = ChunkEnrichment(chunk_id="c2")
        self.assertIsNone(ChunkEnrichment.from_dict(ok.to_dict()).error)

    def test_except_handler_write_does_not_clobber_a_newer_terminal_row(self):
        """User-approved ASK fix: the except Exception handler's terminal
        write is currency-guarded like the failed-candidate block — a
        superseded attempt whose embed fails AFTER a newer job's own
        enrichment already completed must not clobber the newer row."""
        h = _EnrichmentJobHarness(file_id=306, file_hash="abcdef012345")
        fake = FakeLLMClient(
            '{"summary": "fine content", "questions": [], "entities": [], "aliases": []}',
            finish_reason="stop",
        )
        h.attach_client(fake)
        h.processor.embedding_service.embed_batch = AsyncMock(
            side_effect=_newer_terminal_write_then_raise(h.db_path, 306)
        )
        try:
            h.run()
            row = h.row()
        finally:
            h.close()

        self.assertEqual(fake.calls, 1, "candidates generated, then embed failed")
        self.assertEqual(
            row["enrichment_status"], "complete",
            "the newer job's terminal state must survive the old attempt's failure",
        )
        self.assertIsNone(row["enrichment_error"])
        self.assertNotIn("ENRICHMENT_FAILED", row["enrichment_error"] or "")
        self.assertEqual(h.swapped_records, [])


if __name__ == "__main__":
    unittest.main()
