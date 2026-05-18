import asyncio
import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_background_processor_start_assigns_write_semaphore_before_workers():
    from app.services.background_tasks import BackgroundProcessor

    processor = BackgroundProcessor()
    processor.processor = MagicMock()
    processor.processor.pool = None
    created_workers = []

    def fake_create_task(coro, name=None):
        assert processor._write_semaphore is not None
        assert processor.processor._write_semaphore is processor._write_semaphore
        coro.close()
        created_workers.append(name)
        return MagicMock()

    with patch("app.services.background_tasks.settings") as mock_settings:
        mock_settings.ingestion_worker_count = 2
        with patch("app.services.background_tasks.asyncio.create_task", side_effect=fake_create_task):
            await processor.start()

    assert created_workers == ["worker-0", "worker-1"]


@pytest.mark.asyncio
async def test_worker_count_two_vector_store_mutations_are_serialized():
    from app.services.background_tasks import BackgroundProcessor
    from app.services.vector_store import VectorStore

    store = VectorStore()
    active = 0
    max_active = 0
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def guarded_operation(*_args):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if not first_entered.is_set():
            first_entered.set()
            await release_first.wait()
        active -= 1
        return {"vector_write_ms": 0.0, "optimize_ms": 0.0}

    async def guarded_delete(*_args):
        await guarded_operation()
        return 0

    store._add_chunks_unlocked = guarded_operation
    store._delete_by_file_unlocked = guarded_delete

    processor = BackgroundProcessor()
    processor.processor = MagicMock()
    processor.processor.pool = None

    async def process_existing_file(file_id, file_path, vault_id):
        if file_id == 1:
            await store.add_chunks([{"id": "1"}])
        else:
            await store.delete_by_file("1")

    processor.processor.process_existing_file = AsyncMock(side_effect=process_existing_file)

    with patch("app.services.background_tasks.settings") as mock_settings:
        mock_settings.ingestion_worker_count = 2
        await processor.enqueue("first.txt", vault_id=1, file_id=1)
        await processor.enqueue("second.txt", vault_id=1, file_id=2)
        await processor.start()

        await first_entered.wait()
        await asyncio.sleep(0)
        assert active == 1

        release_first.set()
        await processor.queue.join()
        processor.shutdown_event.set()
        await asyncio.wait_for(
            asyncio.gather(*processor._worker_tasks, return_exceptions=True),
            timeout=2,
        )
        processor._running = False

    assert max_active == 1


@pytest.mark.asyncio
async def test_embedding_global_semaphore_limits_batches_across_documents():
    from app.services.embeddings import EmbeddingService

    active = 0
    max_active = 0
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_embed_batch_api(self, texts):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if not first_entered.is_set():
            first_entered.set()
            await release_first.wait()
        active -= 1
        return [[1.0, 2.0] for _ in texts]

    EmbeddingService._global_batch_semaphore = None
    EmbeddingService._global_batch_semaphore_limit = None
    EmbeddingService._global_batch_semaphore_loop = None

    with patch("app.services.embeddings.settings") as mock_settings:
        mock_settings.ollama_embedding_url = "http://localhost:8080/v1/embeddings"
        mock_settings.embedding_model = "test-model"
        mock_settings.embedding_doc_prefix = ""
        mock_settings.embedding_query_prefix = ""
        mock_settings.embedding_batch_size = 1
        mock_settings.embedding_batch_max_retries = 3
        mock_settings.embedding_batch_min_sub_size = 1
        mock_settings.embedding_concurrent_batches = 4
        mock_settings.embedding_global_concurrent_batches = 1

        service_a = EmbeddingService()
        service_b = EmbeddingService()
        with patch.object(EmbeddingService, "_embed_batch_api", fake_embed_batch_api):
            task_a = asyncio.create_task(service_a.embed_batch(["a1", "a2"], batch_size=1))
            task_b = asyncio.create_task(service_b.embed_batch(["b1", "b2"], batch_size=1))
            await first_entered.wait()
            await asyncio.sleep(0)
            assert active == 1

            release_first.set()
            await asyncio.gather(task_a, task_b)

    assert max_active == 1
    EmbeddingService._global_batch_semaphore = None
    EmbeddingService._global_batch_semaphore_limit = None
    EmbeddingService._global_batch_semaphore_loop = None


@pytest.mark.asyncio
async def test_process_file_emits_one_stage_timing_log(tmp_path, caplog):
    from app.services.chunking import ProcessedChunk
    from app.services.document_processor import DocumentProcessor

    file_path = tmp_path / "doc.txt"
    file_path.write_text("hello world", encoding="utf-8")

    pool = MagicMock()
    conn = MagicMock()
    pool.get_connection.return_value = conn

    embedding_service = MagicMock()
    embedding_service.embed_batch = AsyncMock(return_value=([[0.1, 0.2]], []))
    vector_store = MagicMock()
    vector_store.init_table = AsyncMock()
    vector_store.delete_by_file = AsyncMock(return_value=0)
    vector_store.add_chunks = AsyncMock(
        return_value={"vector_write_ms": 1.0, "optimize_ms": 2.0}
    )
    vector_store.count_by_file = AsyncMock(return_value=1)

    processor = DocumentProcessor(
        pool=pool,
        embedding_service=embedding_service,
        vector_store=vector_store,
    )
    chunk = ProcessedChunk(text="hello world", metadata={}, chunk_index=0)

    with patch("app.services.document_processor.settings") as mock_settings:
        mock_settings.contextual_chunking_enabled = False
        mock_settings.parent_retrieval_enabled = True
        mock_settings.parent_window_chars = 20
        mock_settings.reupload_safe_order = False
        mock_settings.embedding_batch_size = 64
        with patch.object(processor, "_check_duplicate", return_value=None), \
            patch.object(processor, "_insert_or_get_file_record", return_value=123), \
            patch.object(processor, "_update_status"), \
            patch.object(processor, "_validate_chunk_sizes"), \
            patch.object(processor, "_is_schema_file", return_value=False), \
            patch.object(processor, "_is_spreadsheet_file", return_value=False), \
            patch.object(
                processor,
                "_process_document_file",
                new=AsyncMock(return_value=([chunk], "hello world")),
            ), \
            patch.object(processor, "_get_chunk_enrichment_service", return_value=None), \
            patch("app.services.document_processor.compute_file_hash", return_value="abc12345"), \
            patch("app.services.document_processor.set_phase"), \
            patch("app.services.document_processor.clear_progress"), \
            patch("app.services.document_processor.set_wiki_pending"), \
            patch("app.services.wiki_store.WikiStore") as mock_wiki_store:
            mock_wiki_store.return_value.create_job.return_value = None
            caplog.set_level(logging.INFO, logger="app.services.document_processor")
            await processor.process_file(str(file_path), vault_id=1)

    timing_logs = [
        record
        for record in caplog.records
        if record.message.startswith("Ingestion stage timings")
    ]
    assert len(timing_logs) == 1
    message = timing_logs[0].message
    timing_values = {
        match.group("field"): float(match.group("value"))
        for match in re.finditer(
            r"(?P<field>[a-z_]+_ms)=(?P<value>\d+(?:\.\d+)?)", message
        )
    }
    for field in (
        "parse_ms",
        "chunk_ms",
        "contextual_ms",
        "parent_window_ms",
        "enrichment_ms",
        "embedding_ms",
        "vector_write_ms",
        "optimize_ms",
        "sqlite_finalize_ms",
    ):
        assert field in timing_values
        assert timing_values[field] >= 0.0

    assert timing_values["vector_write_ms"] >= 1.0
    assert timing_values["optimize_ms"] == 2.0
