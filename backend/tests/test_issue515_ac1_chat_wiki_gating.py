"""Issue #515 acceptance tests — AC1 (WIKI-001).

Chat answers must not persist wiki data when ``wiki_enabled`` is False or
``wiki_compile_on_query`` is False, on BOTH the streaming and non-streaming
paths; with both flags on the job must be created and processed; a job
enqueued while the flags were on must end cancelled (or explicitly
skipped-for-disabled) when the flags are flipped off before the processor
dispatch runs.

Harness notes
-------------
The enqueue happens through ``asyncio.create_task`` on the caller's event
loop (see ``app/api/routes/chat.py`` call sites at the end of
``stream_chat_response``'s event generator and of
``non_stream_chat_response``). Driving those two response helpers directly
inside a test-owned event loop lets us deterministically drain
``chat_routes._background_tasks`` after the answer completes; going through
``TestClient`` instead would leave the spawned task subject to portal
cancellation at request teardown, which would make the DB assertions racy.

The DB / settings / pool setup mirrors ``test_issue514_backend.py``'s
``ChatDocumentScopeRouteTest`` (temp data_dir, pool-cache reset,
init_db + run_migrations, mocked RAG engine emitting sources), and the
worker-policy test drives ``WikiCompileProcessor``'s per-job steps
(claim -> dispatch -> complete) the way ``_poll_loop`` does, minus the
5-second poll sleep, so it stays deterministic.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional heavy deps so importing app modules is cheap (mirrors the
# repo's existing route-test preamble).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

AFOMIS_ANSWER = (
    "AFOMIS stands for Air Force Operational Medicine Information Systems. "
    "Justice Sakyi is the AFOMIS Chief and Major Justin Woods is his deputy."
)

VAULT_ID = 2


def _settings():
    from app.config import settings

    return settings


class ChatWikiGatingTestBase(unittest.IsolatedAsyncioTestCase):
    """Shared temp-DB harness for the AC1 gating tests."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._originals = {"data_dir": _settings().data_dir}
        _settings().data_dir = Path(self._temp_dir)
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _pool, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        import sqlite3

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) "
                "VALUES (?, 'Gating Vault', 'g')",
                (VAULT_ID,),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _pool, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()
        _settings().data_dir = self._originals["data_dir"]
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _make_mock_rag_engine():
        """Mock engine mirroring ChatDocumentScopeRouteTest's recording_query:
        an async generator that emits content then a done chunk with sources
        (sources non-empty so the post-answer enqueue condition holds)."""
        engine = MagicMock()

        async def _query(*args, **kwargs):
            yield {"type": "content", "content": AFOMIS_ANSWER}
            yield {
                "type": "done",
                "sources": [
                    {
                        "file_id": "1",
                        "filename": "handbook.txt",
                        "source_label": "S1",
                    }
                ],
                "memories_used": [],
                "wiki_used": [],
                "kms_used": [],
                "score_type": "distance",
            }

        engine.query = _query
        return engine

    async def _drain_background_tasks(self):
        """Deterministically await every chat background task spawned so far."""
        from app.api.routes import chat as chat_routes

        tasks = list(chat_routes._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _drive_streaming_answer(self):
        """Run stream_chat_response to exhaustion (the enqueue call site lives
        at the end of its event generator) and drain the spawned task."""
        from app.api.routes import chat as chat_routes

        response = chat_routes.stream_chat_response(
            message="Who runs AFOMIS?",
            history=[],
            rag_engine=self._make_mock_rag_engine(),
            vault_id=VAULT_ID,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        self.assertTrue(
            any('"type": "done"' in c or '"type":"done"' in c for c in chunks),
            f"stream never emitted a done event: {chunks!r}",
        )
        await self._drain_background_tasks()
        return chunks

    async def _drive_non_streaming_answer(self):
        from app.api.routes import chat as chat_routes

        response = await chat_routes.non_stream_chat_response(
            message="Who runs AFOMIS?",
            history=[],
            rag_engine=self._make_mock_rag_engine(),
            vault_id=VAULT_ID,
        )
        self.assertIn("AFOMIS", response.content)
        await self._drain_background_tasks()
        return response

    def _wiki_job_rows(self):
        """Read wiki_compile_jobs on a FRESH connection (durable state)."""
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = None
            rows = conn.execute(
                "SELECT id, trigger_type, status FROM wiki_compile_jobs "
                "WHERE vault_id = ?",
                (VAULT_ID,),
            ).fetchall()
            return rows
        finally:
            conn.close()

    def _wiki_derived_counts(self):
        """Count wiki claims/entities/pages rows for the vault (fresh conn)."""
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        try:
            counts = {}
            for table in ("wiki_claims", "wiki_entities", "wiki_pages"):
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE vault_id = ?",
                    (VAULT_ID,),
                ).fetchone()[0]
            return counts
        finally:
            conn.close()

    def _run_processor_once(self):
        """Drive WikiCompileProcessor's per-job steps exactly like one
        _poll_loop iteration (claim -> dispatch -> complete), without the
        poll sleep, and return the claimed job."""
        from app.models.database import get_pool
        from app.services.wiki_compile_processor import WikiCompileProcessor

        pool = get_pool(str(_settings().sqlite_path))
        processor = WikiCompileProcessor(pool)
        claimed = processor._claim_next_job()
        if claimed is None:
            return None
        result = processor._dispatch(claimed)
        processor._complete_job(claimed.id, result)
        return claimed


class TestIssue515Ac1ChatWikiGating(ChatWikiGatingTestBase):
    """AC1 (WIKI-001): chat answers must respect the wiki feature flags."""

    async def test_issue515_ac1_streaming_no_wiki_job_when_flags_off(self):
        """Streaming answers must not create wiki jobs with either flag off."""
        from app.config import settings

        for flag in ("wiki_enabled", "wiki_compile_on_query"):
            with patch.object(settings, flag, False):
                await self._drive_streaming_answer()
            rows = self._wiki_job_rows()
            self.assertEqual(
                rows,
                [],
                f"streaming chat answer persisted wiki data with "
                f"{flag}=False (wiki_compile_jobs rows: {rows!r})",
            )

    async def test_issue515_ac1_non_streaming_no_wiki_job_when_flags_off(self):
        """Non-streaming answers must not create wiki jobs with either flag off."""
        from app.config import settings

        for flag in ("wiki_enabled", "wiki_compile_on_query"):
            with patch.object(settings, flag, False):
                await self._drive_non_streaming_answer()
            rows = self._wiki_job_rows()
            self.assertEqual(
                rows,
                [],
                f"non-streaming chat answer persisted wiki data with "
                f"{flag}=False (wiki_compile_jobs rows: {rows!r})",
            )

    async def test_issue515_ac1_both_flags_on_creates_and_processes_job(self):
        """With both flags on, the answer enqueues a 'query' job and the
        processor turns it into wiki rows (guards the harness plumbing)."""
        rows = self._wiki_job_rows()
        self.assertEqual(rows, [], "no job should exist before the answer")

        await self._drive_non_streaming_answer()

        rows = self._wiki_job_rows()
        self.assertEqual(
            len(rows), 1, f"expected exactly one enqueued query job: {rows!r}"
        )
        self.assertEqual(rows[0][1], "query")
        self.assertEqual(rows[0][2], "pending")

        claimed = self._run_processor_once()
        self.assertIsNotNone(claimed, "processor must claim the pending job")

        import sqlite3

        conn = sqlite3.connect(self._db_path)
        try:
            status = conn.execute(
                "SELECT status FROM wiki_compile_jobs WHERE id = ?",
                (claimed.id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, "completed")
        counts = self._wiki_derived_counts()
        self.assertGreater(
            counts["wiki_entities"] + counts["wiki_claims"],
            0,
            f"processing the query job must produce wiki rows: {counts!r}",
        )

    async def test_issue515_ac1_disable_after_enqueue_cancels_job(self):
        """A job enqueued while the flags were on must end cancelled (or
        explicitly skipped-for-disabled) when the flags are off at dispatch,
        and must not write any wiki page/claim rows."""
        from app.config import settings
        from app.models.database import get_pool
        from app.services.wiki_store import WikiStore

        pool = get_pool(str(settings.sqlite_path))
        with pool.connection() as conn:
            job = WikiStore(conn).create_job(
                vault_id=VAULT_ID,
                trigger_type="query",
                trigger_id="query",
                input_json={
                    "user_query": "Who runs AFOMIS?",
                    "assistant_answer": AFOMIS_ANSWER,
                    "vault_id": VAULT_ID,
                    "doc_sources": [
                        {"file_id": "1", "source_label": "S1"}
                    ],
                    "per_claim_sources": {},
                },
            )

        for flag in ("wiki_enabled", "wiki_compile_on_query"):
            with patch.object(settings, flag, False):
                claimed = self._run_processor_once()
            self.assertIsNotNone(
                claimed, f"processor must claim job with {flag} off"
            )
            self.assertEqual(claimed.id, job.id)

            import sqlite3

            fresh = sqlite3.connect(self._db_path)
            try:
                row = fresh.execute(
                    "SELECT status, result_json FROM wiki_compile_jobs "
                    "WHERE id = ?",
                    (job.id,),
                ).fetchone()
            finally:
                fresh.close()
            status, result_json = row

            if status == "completed":
                # The alternative pinned shape: an explicit skipped-for-disabled
                # completion is acceptable, a real compile is not.
                try:
                    result = json.loads(result_json or "{}")
                except (json.JSONDecodeError, TypeError):
                    result = {}
                self.assertTrue(
                    bool(result.get("skipped"))
                    and "disabl" in str(result.get("reason", "")).lower(),
                    f"job with flags off at dispatch must end cancelled or "
                    f"explicitly skipped-for-disabled, got status=completed "
                    f"result={result!r}",
                )
            else:
                self.assertEqual(
                    status,
                    "cancelled",
                    f"job with flags off at dispatch must end cancelled, "
                    f"got status={status!r}",
                )

            counts = self._wiki_derived_counts()
            self.assertEqual(
                counts["wiki_claims"],
                0,
                f"disabled dispatch must not create wiki claims: {counts!r}",
            )
            self.assertEqual(
                counts["wiki_pages"],
                0,
                f"disabled dispatch must not create wiki pages: {counts!r}",
            )

            # Requeue so the second flag-off iteration has a job to claim.
            with pool.connection() as conn:
                conn.execute(
                    "UPDATE wiki_compile_jobs SET status = 'pending', "
                    "started_at = NULL, completed_at = NULL WHERE id = ?",
                    (job.id,),
                )
                conn.commit()


if __name__ == "__main__":
    unittest.main()
