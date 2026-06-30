"""Regression tests for non-blocking ingestion LLM enrichment."""

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes.settings import SettingsUpdate, _hot_rebind_llm_clients
from app.config import settings
from app.lifespan import select_ingestion_llm_client
from app.models.database import SQLiteConnectionPool, init_db
from app.services.background_tasks import BackgroundProcessor, TaskItem
from app.services.chunk_enrichment import ChunkEnrichmentService
from app.services.document_processor import ProcessedDocument


class TestIngestionLLMRouting(unittest.TestCase):
    def test_select_ingestion_llm_client_uses_instant_client(self):
        instant = object()
        thinking = object()
        app = SimpleNamespace(
            state=SimpleNamespace(
                instant_llm_client=instant,
                thinking_llm_client=thinking,
            )
        )

        self.assertIs(select_ingestion_llm_client(app, "instant"), instant)
        self.assertIs(select_ingestion_llm_client(app, "thinking"), thinking)
        self.assertIsNone(select_ingestion_llm_client(app, "disabled"))

    def test_settings_update_rebinds_running_background_processor(self):
        class FakeBackgroundProcessor:
            def __init__(self):
                self.bound_client = "unset"

            def set_llm_client(self, client):
                self.bound_client = client

        instant = object()
        thinking = object()
        background_processor = FakeBackgroundProcessor()
        app = SimpleNamespace(
            state=SimpleNamespace(
                instant_llm_client=instant,
                thinking_llm_client=thinking,
                background_processor=background_processor,
            )
        )
        original_mode = settings.ingestion_llm_mode
        try:
            settings.ingestion_llm_mode = "disabled"
            _hot_rebind_llm_clients(
                app, SettingsUpdate(ingestion_llm_mode="disabled")
            )
            self.assertIsNone(background_processor.bound_client)

            settings.ingestion_llm_mode = "thinking"
            _hot_rebind_llm_clients(
                app, SettingsUpdate(ingestion_llm_mode="thinking")
            )
            self.assertIs(background_processor.bound_client, thinking)

            settings.ingestion_llm_mode = "instant"
            _hot_rebind_llm_clients(
                app, SettingsUpdate(ingestion_llm_mode="instant")
            )
            self.assertIs(background_processor.bound_client, instant)
        finally:
            settings.ingestion_llm_mode = original_mode


class TestChunkEnrichmentLimits(unittest.TestCase):
    def test_enrichment_generation_uses_bounded_max_tokens(self):
        class FakeLLMClient:
            def __init__(self):
                self.calls = []

            async def chat_completion(self, messages, max_tokens, temperature):
                self.calls.append(
                    {
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    }
                )
                return '{"summary":"short","questions":[],"entities":[],"aliases":[]}'

        client = FakeLLMClient()
        service = ChunkEnrichmentService(client)

        result = asyncio.run(
            service._generate_enrichment(
                chunk_id="file_0",
                text="Important evidence.",
                document_title="doc.txt",
                section="",
            )
        )

        self.assertEqual(result.summary, "short")
        self.assertEqual(client.calls[0]["max_tokens"], 512)
        self.assertEqual(client.calls[0]["temperature"], 0.2)


class TestBackgroundEnrichmentQueue(unittest.TestCase):
    def test_base_index_completes_before_enrichment_is_queued(self):
        events = []

        class FakeProcessor:
            async def process_existing_file(self, file_id, file_path, vault_id):
                events.append("indexed")
                return ProcessedDocument(
                    file_id=file_id,
                    chunks=[object()],
                    document_text="base text",
                    file_hash="abcdef012345",
                    file_path=file_path,
                    vault_id=vault_id,
                )

            def should_enqueue_enrichment(self, chunks, vault_id=None, file_id=None):
                return True

            def set_enrichment_status(self, file_id, status, error_message=None):
                events.append(f"enrichment:{status}")

        async def run_case():
            processor = BackgroundProcessor()
            processor.processor = FakeProcessor()
            await processor._process_task(
                TaskItem(file_path="doc.txt", vault_id=1, file_id=123)
            )
            queued = await processor.enrichment_queue.get()
            processor.enrichment_queue.task_done()
            return queued

        queued = asyncio.run(run_case())

        self.assertEqual(events, ["indexed", "enrichment:pending"])
        self.assertEqual(queued.file_id, 123)
        self.assertEqual(queued.document_text, "base text")

    def test_stop_drains_late_enrichment_enqueue_before_worker_exit(self):
        events = []

        async def run_case():
            started = asyncio.Event()
            release = asyncio.Event()

            class FakeProcessor:
                pool = None

                async def process_existing_file(self, file_id, file_path, vault_id):
                    started.set()
                    await release.wait()
                    events.append("indexed")
                    return ProcessedDocument(
                        file_id=file_id,
                        chunks=[object()],
                        document_text="base text",
                        file_hash="abcdef012345",
                        file_path=file_path,
                        vault_id=vault_id,
                    )

                def should_enqueue_enrichment(self, chunks, vault_id=None, file_id=None):
                    return True

                def set_enrichment_status(self, file_id, status, error_message=None):
                    events.append(f"enrichment:{status}")

                async def run_enrichment_job(self, **kwargs):
                    events.append("enrichment:ran")

            processor = BackgroundProcessor()
            processor.processor = FakeProcessor()
            await processor.start()
            await processor.enqueue(file_path="doc.txt", vault_id=1, file_id=123)
            await started.wait()
            original_join = processor.queue.join
            stop_waiting_for_ingestion = asyncio.Event()

            async def tracked_join():
                stop_waiting_for_ingestion.set()
                return await original_join()

            processor.queue.join = tracked_join
            stop_task = asyncio.create_task(processor.stop(timeout=5))
            await stop_waiting_for_ingestion.wait()
            release.set()
            await stop_task

        asyncio.run(run_case())

        self.assertEqual(events, ["indexed", "enrichment:pending", "enrichment:ran"])

    def test_startup_recovery_marks_interrupted_enrichment_error(self):
        """Regression: crashed post-index enrichment must not stay processing forever."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "test.db")
            init_db(db_path)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'Default', '')"
            )
            conn.execute(
                """
                INSERT INTO files
                    (id, vault_id, file_path, file_name, file_hash, file_size, status,
                     chunk_count, enrichment_status)
                VALUES (321, 1, ?, 'doc.txt', 'hash', 1, 'indexed', 1, 'pending')
                """,
                (os.path.join(temp_dir, "doc.txt"),),
            )
            conn.execute(
                """
                INSERT INTO files
                    (id, vault_id, file_path, file_name, file_hash, file_size, status,
                     chunk_count, enrichment_status)
                VALUES (322, 1, ?, 'doc2.txt', 'hash2', 1, 'indexed', 1, 'processing')
                """,
                (os.path.join(temp_dir, "doc2.txt"),),
            )
            conn.commit()
            conn.close()

            pool = SQLiteConnectionPool(db_path, max_size=2)
            try:
                processor = BackgroundProcessor(pool=pool)
                asyncio.run(processor._recover_stranded_enrichment_rows())
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, status, enrichment_status, enrichment_error FROM files WHERE id IN (321, 322)"
                ).fetchall()
                conn.close()
            finally:
                pool.close_all()

        self.assertEqual({row["id"] for row in rows}, {321, 322})
        for row in rows:
            self.assertEqual(row["status"], "indexed")
            self.assertEqual(row["enrichment_status"], "error")
            self.assertIn("before completion", row["enrichment_error"])


if __name__ == "__main__":
    unittest.main()
