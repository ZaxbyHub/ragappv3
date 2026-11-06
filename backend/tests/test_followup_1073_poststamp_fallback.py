"""Post-stamp complete-writer NOT_CURRENT fallback leg (plan-critic Round 4 ask).

Issues #1075/#1076. GREEN at the fix head; the mutation that drops the
NOT_CURRENT fallback on the no-content arm turns this RED (the stale marker
would never land and the row would stay 'processing'). Ordinary regression
test (not part of the frozen checkpoint).
"""

import asyncio  # noqa: F401
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_followup_1073_status_clobber import (  # noqa: E402
    _enrichment_row,
    _make_db,
    _wait_for_status,
)

from app.models.database import SQLiteConnectionPool  # noqa: E402
from app.services.document_processor import DocumentProcessor  # noqa: E402


class _NoContentService:
    """Enrichment service whose candidates all return empty enrichments."""

    async def enrich_chunks(self, chunk_dicts, document_title: str = ""):
        from app.services.chunk_enrichment import ChunkEnrichment

        return [
            ChunkEnrichment(chunk_id=c["chunk_uid"]) for c in chunk_dicts
        ]


class TestPostStampNotCurrentFallback(unittest.IsolatedAsyncioTestCase):
    async def test_no_content_arm_hash_rotated_after_stamp_marks_stale(self):
        """The no-content complete arm, superseded mid-flight, marks stale.

        Flow: the job stamps 'processing'; the service double rotates the file
        hash (a newer ingest owns the row) and returns no-content enrichments;
        the guarded complete write matches zero rows (NOT_CURRENT); the
        post-stamp arm must fall back to the stale marker, so the row reads
        'error' with the stale message — never a clobbering 'complete'.
        """
        tmp = Path(__file__).parent / ".rag-poststamp" / uuid.uuid4().hex
        tmp.mkdir(parents=True)
        try:
            db_path, file_id, vault_id = _make_db(
                tmp, enrichment_status="pending", enrichment_error=None
            )
            pool = SQLiteConnectionPool(db_path)
            import types

            stub = types.SimpleNamespace(
                pool=pool,
                _write_semaphore=asyncio.Semaphore(1),
                _detached_status_tasks=set(),
            )
            stub.set_enrichment_status = lambda *a, **k: DocumentProcessor.set_enrichment_status(stub, *a, **k)
            stub._mark_enrichment_stale_if_current_job = lambda *a, **k: DocumentProcessor._mark_enrichment_stale_if_current_job(stub, *a, **k)
            stub._is_enrichment_job_current = lambda *a, **k: DocumentProcessor._is_enrichment_job_current(stub, *a, **k)

            service = _NoContentService()
            real_enrich = service.enrich_chunks

            async def rotating_enrich(chunk_dicts, document_title: str = ""):
                import sqlite3

                conn = sqlite3.connect(db_path)
                conn.execute(
                    "UPDATE files SET file_hash = 'rotated-newer' WHERE id = ?",
                    (file_id,),
                )
                conn.commit()
                conn.close()
                return await real_enrich(chunk_dicts, document_title)

            service.enrich_chunks = rotating_enrich
            stub._get_chunk_enrichment_service = lambda vault_id, file_id: service
            chunk = types.SimpleNamespace(
                text="a chunk body with real content",
                metadata={"chunk_scale": "default", "total_chunks": 1},
            )
            stub._candidate_chunks_for_enrichment = lambda file_id, chunks: [(chunk, "f1_0")]

            await DocumentProcessor.run_enrichment_job(
                stub,
                file_id=file_id,
                file_path="/x/a.png",
                vault_id=vault_id,
                file_hash="hash1",
                chunks=[chunk],
                document_text="a chunk body with real content",
            )
            row = _enrichment_row(pool, file_id)
            pool.close_all()
            self.assertEqual(row["enrichment_status"], "error")
            self.assertIn("became stale", row["enrichment_error"] or "")
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
