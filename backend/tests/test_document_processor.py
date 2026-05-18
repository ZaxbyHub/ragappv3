"""Unit tests for DocumentProcessor using SQL file path."""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db
from app.services.chunk_enrichment import ChunkEnrichment
from app.services.chunking import ProcessedChunk
from app.services.document_processor import (
    DocumentProcessingError,
    DocumentProcessor,
    DuplicateFileError,
    ProcessedDocument,
)


class TestDocumentProcessor(unittest.TestCase):
    """Test cases for DocumentProcessor with SQL file processing."""

    def setUp(self):
        """Create temporary database and SQL file for each test."""
        # Create temp directory for all test files
        self.temp_dir = tempfile.mkdtemp()

        # Create temp sqlite file
        self.temp_db_path = os.path.join(self.temp_dir, 'test.db')

        # Initialize the database
        init_db(self.temp_db_path)
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'Default', '')"
        )
        conn.commit()
        conn.close()

        # Create temp .sql file with CREATE TABLE statement
        self.sql_file_path = os.path.join(self.temp_dir, 'test_schema.sql')
        sql_content = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""
        with open(self.sql_file_path, 'w', encoding='utf-8') as f:
            f.write(sql_content)

        # Monkeypatch settings.data_dir so sqlite_path resolves to temp path
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self.temp_dir)

        # Create test pool
        self.test_pool = SQLiteConnectionPool(self.temp_db_path, max_size=2)

        # Create DocumentProcessor instance with pool
        self.processor = DocumentProcessor(chunk_size_chars=2000, chunk_overlap_chars=200, pool=self.test_pool)

    def tearDown(self):
        """Clean up temporary files."""
        settings.data_dir = self._original_data_dir

        # Close test pool
        self.test_pool.close_all()

        # Remove temp files
        if os.path.exists(self.sql_file_path):
            os.remove(self.sql_file_path)
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)
        if os.path.exists(self.temp_dir):
            os.rmdir(self.temp_dir)

    def _insert_indexed_file(self, file_id=77, file_hash="abcdef012345"):
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute(
            """
            INSERT INTO files
                (id, vault_id, file_path, file_name, file_hash, file_size, status, chunk_count)
            VALUES (?, 1, ?, 'test_schema.sql', ?, 1, 'indexed', 1)
            """,
            (file_id, self.sql_file_path, file_hash),
        )
        conn.commit()
        conn.close()

    def test_reprocessing_existing_file_clears_stale_enrichment_state(self):
        """Restarting ingestion for the same path must not expose old enrichment state."""
        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """
                INSERT INTO files
                    (id, vault_id, file_path, file_name, file_hash, file_size, status,
                     chunk_count, enrichment_status, enrichment_error, enrichment_updated_at)
                VALUES (
                    71, 1, ?, 'test_schema.sql', 'oldhash', 1, 'indexed',
                    1, 'error', 'previous enrichment failed', '2026-01-01T00:00:00Z'
                )
                """,
                (self.sql_file_path,),
            )
            conn.commit()

            file_id = self.processor._insert_or_get_file_record(
                self.sql_file_path,
                "newhash",
                conn,
                vault_id=1,
            )
            row = conn.execute(
                """
                SELECT status, file_hash, enrichment_status, enrichment_error, enrichment_updated_at
                FROM files
                WHERE id = ?
                """,
                (file_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(file_id, 71)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["file_hash"], "newhash")
        self.assertIsNone(row["enrichment_status"])
        self.assertIsNone(row["enrichment_error"])
        self.assertIsNone(row["enrichment_updated_at"])

    def test_process_file_returns_valid_result(self):
        """Test that process_file returns valid ProcessedDocument with SQL file."""
        # Process the SQL file
        result = asyncio.run(
            self.processor.process_file(self.sql_file_path, vault_id=1)
        )

        # Assert result is ProcessedDocument
        self.assertIsInstance(result, ProcessedDocument)

        # Assert file_id is int
        self.assertIsInstance(result.file_id, int)
        self.assertGreater(result.file_id, 0)

        # Assert chunks list is not empty
        self.assertIsInstance(result.chunks, list)
        self.assertGreater(len(result.chunks), 0)

    def test_process_file_updates_db_status(self):
        """Test that process_file updates DB status to indexed with chunk_count."""
        # Process the SQL file
        result = asyncio.run(
            self.processor.process_file(self.sql_file_path, vault_id=1)
        )

        # Query database to verify status
        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT status, chunk_count FROM files WHERE id = ?",
            (result.file_id,)
        )
        row = cursor.fetchone()
        conn.close()

        # Assert status is 'indexed'
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'indexed')

        # Assert chunk_count matches number of chunks
        self.assertEqual(row['chunk_count'], len(result.chunks))

    def test_process_file_raises_duplicate_error_on_second_call(self):
        """Test that second call with same file raises DuplicateFileError."""
        # Process the SQL file first time
        asyncio.run(self.processor.process_file(self.sql_file_path, vault_id=1))

        # Second call should raise DuplicateFileError
        with self.assertRaises(DuplicateFileError):
            asyncio.run(self.processor.process_file(self.sql_file_path, vault_id=1))

    def test_process_file_extracts_correct_chunks(self):
        """Test that SQL file is correctly parsed into chunks."""
        # Process the SQL file
        result = asyncio.run(
            self.processor.process_file(self.sql_file_path, vault_id=1)
        )

        # Should have 2 chunks (users table and posts table)
        self.assertEqual(len(result.chunks), 2)

        # Verify chunk content contains expected table names
        chunk_texts = [chunk.text for chunk in result.chunks]
        self.assertTrue(
            any('users' in text for text in chunk_texts),
            "Expected one chunk to contain 'users' table"
        )
        self.assertTrue(
            any('posts' in text for text in chunk_texts),
            "Expected one chunk to contain 'posts' table"
        )

    def test_process_file_marks_error_when_vector_rows_not_visible(self):
        """Visibility failure after vector writes must not mark SQLite indexed."""

        class FakeEmbeddingService:
            MAX_TEXT_LENGTH = 8192
            embedding_doc_prefix = ""

            async def embed_batch(self, texts, fail_fast=False):
                return ([[0.1, 0.2, 0.3, 0.4] for _ in texts], [])

        class FakeVectorStore:
            def __init__(self):
                self.records = []

            async def init_table(self, embedding_dim):
                self.embedding_dim = embedding_dim

            async def add_chunks(self, records):
                self.records.extend(records)

            async def delete_old_generation_by_file(self, file_id, new_hash_short):
                return 0

            async def delete_by_file(self, file_id):
                return 0

            async def count_by_file(self, file_id):
                return 0

        original_reupload_safe_order = settings.reupload_safe_order
        settings.reupload_safe_order = True
        self.processor.embedding_service = FakeEmbeddingService()
        self.processor.vector_store = FakeVectorStore()

        try:
            with self.assertRaises(DocumentProcessingError) as ctx:
                asyncio.run(self.processor.process_file(self.sql_file_path, vault_id=1))
        finally:
            settings.reupload_safe_order = original_reupload_safe_order

        self.assertIn("zero LanceDB rows", str(ctx.exception))

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE file_path = ?",
            (self.sql_file_path,),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "error")
        self.assertIn("zero LanceDB rows", row["error_message"])

    def test_base_vector_record_preserves_raw_text_without_enrichment(self):
        """Initial indexing stores raw evidence and does not add synthetic search text."""
        chunk = ProcessedChunk(
            text="base chunk text",
            metadata={"chunk_scale": "default", "total_chunks": 1},
            chunk_index=0,
            raw_text="original evidence text",
        )

        record = self.processor._build_vector_record(
            file_id=42,
            vault_id=1,
            file_hash="abcdef012345",
            chunk=chunk,
            embedding=[0.1, 0.2],
            sparse_emb=None,
            document_text="original evidence text",
        )

        metadata = json.loads(record["metadata"])
        self.assertEqual(record["text"], "base chunk text")
        self.assertEqual(metadata["raw_text"], "original evidence text")
        self.assertNotIn("enrichment", metadata)

    def test_enriched_vector_record_adds_searchable_text_and_keeps_raw_text(self):
        """Post-index enrichment is searchable while citations keep original evidence."""
        chunk = ProcessedChunk(
            text="contract renewal clause",
            metadata={"chunk_scale": "default", "total_chunks": 1},
            chunk_index=0,
            raw_text="raw contract clause",
        )
        enrichment = ChunkEnrichment(
            chunk_id="42_0",
            summary="Renewal notice terms.",
            questions=["When does renewal notice happen?"],
            entities=["Renewal", "Notice"],
            aliases=["extension"],
        )

        record = self.processor._build_vector_record(
            file_id=42,
            vault_id=1,
            file_hash="abcdef012345",
            chunk=chunk,
            embedding=[0.1, 0.2],
            sparse_emb=None,
            document_text="raw contract clause",
            enrichment=enrichment,
        )

        metadata = json.loads(record["metadata"])
        self.assertIn("Summary: Renewal notice terms.", record["text"])
        self.assertIn("Questions: When does renewal notice happen?", record["text"])
        self.assertIn("Entities: Renewal Notice extension", record["text"])
        self.assertEqual(metadata["raw_text"], "raw contract clause")
        self.assertEqual(metadata["enrichment"]["summary"], "Renewal notice terms.")

    def test_search_text_with_empty_enrichment_returns_original_text(self):
        """Empty enrichment payloads must not change searchable evidence text."""
        chunk = ProcessedChunk(
            text="original clause",
            metadata={"chunk_scale": "default"},
            chunk_index=0,
        )
        enrichment = ChunkEnrichment(
            chunk_id="42_0",
            summary="",
            questions=[],
            entities=[],
            aliases=[],
        )

        text = self.processor._search_text_with_enrichment(chunk, enrichment)

        self.assertEqual(text, "original clause")

    def test_search_text_with_enrichment_bounds_each_field(self):
        """Long enrichment fields are bounded before being appended to search text."""
        chunk = ProcessedChunk(
            text="base evidence",
            metadata={"chunk_scale": "default"},
            chunk_index=0,
        )
        enrichment = ChunkEnrichment(
            chunk_id="42_0",
            summary="s" * 1200,
            questions=["q" * 300],
            entities=["e" * 150],
            aliases=["a" * 150],
        )

        text = self.processor._search_text_with_enrichment(chunk, enrichment)

        self.assertIn("Summary: " + ("s" * 1000), text)
        self.assertNotIn("s" * 1001, text)
        self.assertIn("Questions: " + ("q" * 240), text)
        self.assertNotIn("q" * 241, text)
        self.assertIn("Entities: " + ("e" * 120) + " " + ("a" * 120), text)
        self.assertNotIn("e" * 121, text)
        self.assertNotIn("a" * 121, text)

    def test_search_text_with_entities_only_adds_entities_block(self):
        """Entities-only enrichment still contributes searchable aliases."""
        chunk = ProcessedChunk(
            text="base evidence",
            metadata={"chunk_scale": "default"},
            chunk_index=0,
        )
        enrichment = ChunkEnrichment(
            chunk_id="42_0",
            entities=["Acme"],
            aliases=["ACM"],
        )

        text = self.processor._search_text_with_enrichment(chunk, enrichment)

        self.assertEqual(text, "base evidence\nEntities: Acme ACM")

    def test_search_text_with_enrichment_caps_list_counts(self):
        """Search text uses bounded list counts for questions, entities, and aliases."""
        chunk = ProcessedChunk(
            text="base evidence",
            metadata={"chunk_scale": "default"},
            chunk_index=0,
        )
        enrichment = ChunkEnrichment(
            chunk_id="42_0",
            questions=[f"q{i}" for i in range(7)],
            entities=[f"entity{i}" for i in range(12)],
            aliases=[f"alias{i}" for i in range(12)],
        )

        text = self.processor._search_text_with_enrichment(chunk, enrichment)

        self.assertIn("q4", text)
        self.assertNotIn("q5", text)
        self.assertIn("entity9", text)
        self.assertNotIn("entity10", text)
        self.assertIn("alias9", text)
        self.assertNotIn("alias10", text)

    def test_should_enqueue_enrichment_false_without_enabled_llm_or_services(self):
        """Enrichment queueing requires the feature flag and all runtime services."""
        chunk = ProcessedChunk(
            text="base evidence",
            metadata={"chunk_scale": "default"},
            chunk_index=0,
        )
        original_enabled = settings.chunk_enrichment_enabled
        try:
            settings.chunk_enrichment_enabled = False
            self.processor._llm_client = object()
            self.processor.embedding_service = object()
            self.processor.vector_store = object()
            self.assertFalse(self.processor.should_enqueue_enrichment([chunk]))

            settings.chunk_enrichment_enabled = True
            self.processor._llm_client = None
            self.assertFalse(self.processor.should_enqueue_enrichment([chunk]))

            self.processor._llm_client = object()
            self.processor.embedding_service = None
            self.assertFalse(self.processor.should_enqueue_enrichment([chunk]))

            self.processor.embedding_service = object()
            self.processor.vector_store = None
            self.assertFalse(self.processor.should_enqueue_enrichment([chunk]))

            self.processor.vector_store = object()
            self.assertFalse(self.processor.should_enqueue_enrichment([]))
        finally:
            settings.chunk_enrichment_enabled = original_enabled

    def test_run_enrichment_job_completes_when_feature_disabled(self):
        """Disabled enrichment marks the optional job complete without LLM work."""
        self._insert_indexed_file(file_id=72)
        chunk = ProcessedChunk(
            text="indexed text",
            metadata={"chunk_scale": "default", "total_chunks": 1},
            chunk_index=0,
        )
        original_enabled = settings.chunk_enrichment_enabled
        settings.chunk_enrichment_enabled = False
        try:
            asyncio.run(
                self.processor.run_enrichment_job(
                    file_id=72,
                    file_path=self.sql_file_path,
                    vault_id=1,
                    file_hash="abcdef012345",
                    chunks=[chunk],
                    document_text="indexed text",
                )
            )
        finally:
            settings.chunk_enrichment_enabled = original_enabled

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, enrichment_status, enrichment_error FROM files WHERE id = 72"
        ).fetchone()
        conn.close()

        self.assertEqual(row["status"], "indexed")
        self.assertEqual(row["enrichment_status"], "complete")
        self.assertIsNone(row["enrichment_error"])

    def test_run_enrichment_job_completes_with_no_candidate_chunks(self):
        """No candidate chunks is a complete optional enrichment no-op."""
        self._insert_indexed_file(file_id=73)
        original_enabled = settings.chunk_enrichment_enabled
        settings.chunk_enrichment_enabled = True
        self.processor._llm_client = object()
        try:
            asyncio.run(
                self.processor.run_enrichment_job(
                    file_id=73,
                    file_path=self.sql_file_path,
                    vault_id=1,
                    file_hash="abcdef012345",
                    chunks=[],
                    document_text="indexed text",
                )
            )
        finally:
            settings.chunk_enrichment_enabled = original_enabled

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, enrichment_status, enrichment_error FROM files WHERE id = 73"
        ).fetchone()
        conn.close()

        self.assertEqual(row["status"], "indexed")
        self.assertEqual(row["enrichment_status"], "complete")
        self.assertIsNone(row["enrichment_error"])

    def test_run_enrichment_job_completes_when_llm_returns_no_content(self):
        """Empty enrichment output does not re-embed and still completes the optional job."""

        class EmptyEnrichmentService:
            async def enrich_chunks(self, chunks, document_title=""):
                return [
                    ChunkEnrichment(
                        chunk_id=chunks[0]["chunk_uid"],
                        summary="",
                        questions=[],
                        entities=[],
                        aliases=[],
                    )
                ]

        class FailingIfCalledEmbeddingService:
            MAX_TEXT_LENGTH = 8192
            embedding_doc_prefix = ""

            async def embed_batch(self, texts, fail_fast=False):
                raise AssertionError("empty enrichment should not be embedded")

        self._insert_indexed_file(file_id=74)
        chunk = ProcessedChunk(
            text="indexed text",
            metadata={"chunk_scale": "default", "total_chunks": 1},
            chunk_index=0,
        )
        original_enabled = settings.chunk_enrichment_enabled
        settings.chunk_enrichment_enabled = True
        self.processor._llm_client = object()
        self.processor.embedding_service = FailingIfCalledEmbeddingService()
        self.processor._chunk_enrichment_service = EmptyEnrichmentService()
        try:
            asyncio.run(
                self.processor.run_enrichment_job(
                    file_id=74,
                    file_path=self.sql_file_path,
                    vault_id=1,
                    file_hash="abcdef012345",
                    chunks=[chunk],
                    document_text="indexed text",
                )
            )
        finally:
            settings.chunk_enrichment_enabled = original_enabled

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, enrichment_status, enrichment_error FROM files WHERE id = 74"
        ).fetchone()
        conn.close()

        self.assertEqual(row["status"], "indexed")
        self.assertEqual(row["enrichment_status"], "complete")
        self.assertIsNone(row["enrichment_error"])

    def test_run_enrichment_job_records_error_when_embedding_count_mismatches(self):
        """Embedding result mismatch is an enrichment error, not an indexing error."""

        class FakeEnrichmentService:
            async def enrich_chunks(self, chunks, document_title=""):
                return [
                    ChunkEnrichment(
                        chunk_id=chunks[0]["chunk_uid"],
                        summary="fresh enrichment",
                    )
                ]

        class MismatchedEmbeddingService:
            MAX_TEXT_LENGTH = 8192
            embedding_doc_prefix = ""

            async def embed_batch(self, texts, fail_fast=False):
                return ([], [])

        self._insert_indexed_file(file_id=75)
        chunk = ProcessedChunk(
            text="indexed text",
            metadata={"chunk_scale": "default", "total_chunks": 1},
            chunk_index=0,
        )
        original_enabled = settings.chunk_enrichment_enabled
        settings.chunk_enrichment_enabled = True
        self.processor._llm_client = object()
        self.processor.embedding_service = MismatchedEmbeddingService()
        self.processor._chunk_enrichment_service = FakeEnrichmentService()
        try:
            asyncio.run(
                self.processor.run_enrichment_job(
                    file_id=75,
                    file_path=self.sql_file_path,
                    vault_id=1,
                    file_hash="abcdef012345",
                    chunks=[chunk],
                    document_text="indexed text",
                )
            )
        finally:
            settings.chunk_enrichment_enabled = original_enabled

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, enrichment_status, enrichment_error FROM files WHERE id = 75"
        ).fetchone()
        conn.close()

        self.assertEqual(row["status"], "indexed")
        self.assertEqual(row["enrichment_status"], "error")
        self.assertIn("Enriched embedding count mismatch", row["enrichment_error"])

    def test_post_index_enrichment_failure_does_not_change_indexed_status(self):
        """A failed enrichment job records enrichment error but leaves file indexed."""

        class FailingEnrichmentService:
            async def enrich_chunks(self, chunks, document_title=""):
                raise RuntimeError("LLM offline")

        conn = sqlite3.connect(self.temp_db_path)
        conn.execute(
            """
            INSERT INTO files
                (id, vault_id, file_path, file_name, file_hash, file_size, status, chunk_count)
            VALUES (77, 1, ?, 'test_schema.sql', 'abcdef012345', 1, 'indexed', 1)
            """,
            (self.sql_file_path,),
        )
        conn.commit()
        conn.close()

        chunk = ProcessedChunk(
            text="indexed text",
            metadata={"chunk_scale": "default", "total_chunks": 1},
            chunk_index=0,
        )
        original_enabled = settings.chunk_enrichment_enabled
        settings.chunk_enrichment_enabled = True
        self.processor._llm_client = object()
        self.processor._chunk_enrichment_service = FailingEnrichmentService()
        try:
            asyncio.run(
                self.processor.run_enrichment_job(
                    file_id=77,
                    file_path=self.sql_file_path,
                    vault_id=1,
                    file_hash="abcdef012345",
                    chunks=[chunk],
                    document_text="indexed text",
                )
            )
        finally:
            settings.chunk_enrichment_enabled = original_enabled

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, error_message, enrichment_status, enrichment_error FROM files WHERE id = 77"
        ).fetchone()
        conn.close()

        self.assertEqual(row["status"], "indexed")
        self.assertIsNone(row["error_message"])
        self.assertEqual(row["enrichment_status"], "error")
        self.assertIn("LLM offline", row["enrichment_error"])

    def test_cancelled_enrichment_marks_error_and_preserves_indexed_status(self):
        """Regression: cancellation after processing starts must not leave status stuck."""

        async def run_case():
            started = asyncio.Event()

            class BlockingEnrichmentService:
                async def enrich_chunks(self, chunks, document_title=""):
                    started.set()
                    await asyncio.Event().wait()

            conn = sqlite3.connect(self.temp_db_path)
            conn.execute(
                """
                INSERT INTO files
                    (id, vault_id, file_path, file_name, file_hash, file_size, status, chunk_count)
                VALUES (78, 1, ?, 'test_schema.sql', 'abcdef012345', 1, 'indexed', 1)
                """,
                (self.sql_file_path,),
            )
            conn.commit()
            conn.close()

            chunk = ProcessedChunk(
                text="indexed text",
                metadata={"chunk_scale": "default", "total_chunks": 1},
                chunk_index=0,
            )
            original_enabled = settings.chunk_enrichment_enabled
            settings.chunk_enrichment_enabled = True
            self.processor._llm_client = object()
            self.processor._chunk_enrichment_service = BlockingEnrichmentService()
            try:
                task = asyncio.create_task(
                    self.processor.run_enrichment_job(
                        file_id=78,
                        file_path=self.sql_file_path,
                        vault_id=1,
                        file_hash="abcdef012345",
                        chunks=[chunk],
                        document_text="indexed text",
                    )
                )
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                settings.chunk_enrichment_enabled = original_enabled

        asyncio.run(run_case())

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, enrichment_status, enrichment_error FROM files WHERE id = 78"
        ).fetchone()
        conn.close()

        self.assertEqual(row["status"], "indexed")
        self.assertEqual(row["enrichment_status"], "error")
        self.assertIn("cancelled", row["enrichment_error"])

    def test_stale_enrichment_job_does_not_replace_current_vectors(self):
        """Queued enrichment for an old hash must not overwrite newer/deleted vectors."""

        class FakeEnrichmentService:
            async def enrich_chunks(self, chunks, document_title=""):
                return [
                    ChunkEnrichment(
                        chunk_id=chunks[0]["chunk_uid"],
                        summary="stale enrichment",
                    )
                ]

        class FakeEmbeddingService:
            MAX_TEXT_LENGTH = 8192
            embedding_doc_prefix = ""

            async def embed_batch(self, texts, fail_fast=False):
                return ([[0.1, 0.2] for _ in texts], [])

        class FakeVectorStore:
            def __init__(self):
                self.swapped = False

            async def add_chunks_then_delete_ids(self, records, old_ids):
                self.swapped = True

        conn = sqlite3.connect(self.temp_db_path)
        conn.execute(
            """
            INSERT INTO files
                (id, vault_id, file_path, file_name, file_hash, file_size, status, chunk_count)
            VALUES (88, 1, ?, 'test_schema.sql', 'newhash', 1, 'indexed', 1)
            """,
            (self.sql_file_path,),
        )
        conn.commit()
        conn.close()

        chunk = ProcessedChunk(
            text="old indexed text",
            metadata={"chunk_scale": "default", "total_chunks": 1},
            chunk_index=0,
        )
        vector_store = FakeVectorStore()
        original_enabled = settings.chunk_enrichment_enabled
        settings.chunk_enrichment_enabled = True
        self.processor._llm_client = object()
        self.processor.embedding_service = FakeEmbeddingService()
        self.processor.vector_store = vector_store
        self.processor._chunk_enrichment_service = FakeEnrichmentService()
        try:
            asyncio.run(
                self.processor.run_enrichment_job(
                    file_id=88,
                    file_path=self.sql_file_path,
                    vault_id=1,
                    file_hash="oldhash",
                    chunks=[chunk],
                    document_text="old indexed text",
                )
            )
        finally:
            settings.chunk_enrichment_enabled = original_enabled

        self.assertFalse(vector_store.swapped)

    def test_midflight_stale_enrichment_marks_terminal_without_vector_swap(self):
        """Regression: stale detection after processing starts must not stay processing."""

        class FakeEnrichmentService:
            async def enrich_chunks(self, chunks, document_title=""):
                return [
                    ChunkEnrichment(
                        chunk_id=chunks[0]["chunk_uid"],
                        summary="fresh enrichment",
                    )
                ]

        class StalingEmbeddingService:
            MAX_TEXT_LENGTH = 8192
            embedding_doc_prefix = ""

            async def embed_batch(inner_self, texts, fail_fast=False):
                conn = sqlite3.connect(self.temp_db_path)
                conn.execute(
                    "UPDATE files SET file_hash = ? WHERE id = ?",
                    ("newhash", 79),
                )
                conn.commit()
                conn.close()
                return ([[0.1, 0.2] for _ in texts], [])

        class FakeVectorStore:
            def __init__(self):
                self.swapped = False

            async def add_chunks_then_delete_ids(self, records, old_ids):
                self.swapped = True

        self._insert_indexed_file(file_id=79)
        chunk = ProcessedChunk(
            text="indexed text",
            metadata={"chunk_scale": "default", "total_chunks": 1},
            chunk_index=0,
        )
        vector_store = FakeVectorStore()
        original_enabled = settings.chunk_enrichment_enabled
        settings.chunk_enrichment_enabled = True
        self.processor._llm_client = object()
        self.processor.embedding_service = StalingEmbeddingService()
        self.processor.vector_store = vector_store
        self.processor._chunk_enrichment_service = FakeEnrichmentService()
        try:
            asyncio.run(
                self.processor.run_enrichment_job(
                    file_id=79,
                    file_path=self.sql_file_path,
                    vault_id=1,
                    file_hash="abcdef012345",
                    chunks=[chunk],
                    document_text="indexed text",
                )
            )
        finally:
            settings.chunk_enrichment_enabled = original_enabled

        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, file_hash, enrichment_status, enrichment_error FROM files WHERE id = 79"
        ).fetchone()
        conn.close()

        self.assertFalse(vector_store.swapped)
        self.assertEqual(row["status"], "indexed")
        self.assertEqual(row["file_hash"], "newhash")
        self.assertEqual(row["enrichment_status"], "error")
        self.assertIn("stale", row["enrichment_error"])

    def test_enrichment_swap_uses_add_first_generation_ids(self):
        """Enrichment writes enriched rows before deleting base row IDs."""

        class FakeEnrichmentService:
            async def enrich_chunks(self, chunks, document_title=""):
                return [
                    ChunkEnrichment(
                        chunk_id=chunks[0]["chunk_uid"],
                        summary="fresh enrichment",
                    )
                ]

        class FakeEmbeddingService:
            MAX_TEXT_LENGTH = 8192
            embedding_doc_prefix = ""

            async def embed_batch(self, texts, fail_fast=False):
                return ([[0.1, 0.2] for _ in texts], [])

        class FakeVectorStore:
            def __init__(self):
                self.records = None
                self.old_ids = None

            async def add_chunks_then_delete_ids(self, records, old_ids):
                self.records = records
                self.old_ids = old_ids

            async def count_by_file(self, file_id):
                return 1

        conn = sqlite3.connect(self.temp_db_path)
        conn.execute(
            """
            INSERT INTO files
                (id, vault_id, file_path, file_name, file_hash, file_size, status, chunk_count)
            VALUES (89, 1, ?, 'test_schema.sql', 'abcdef012345', 1, 'indexed', 1)
            """,
            (self.sql_file_path,),
        )
        conn.commit()
        conn.close()

        chunk = ProcessedChunk(
            text="indexed text",
            metadata={"chunk_scale": "default", "total_chunks": 1},
            chunk_index=0,
        )
        vector_store = FakeVectorStore()
        original_enabled = settings.chunk_enrichment_enabled
        original_safe_order = settings.reupload_safe_order
        settings.chunk_enrichment_enabled = True
        settings.reupload_safe_order = True
        self.processor._llm_client = object()
        self.processor.embedding_service = FakeEmbeddingService()
        self.processor.vector_store = vector_store
        self.processor._chunk_enrichment_service = FakeEnrichmentService()
        try:
            asyncio.run(
                self.processor.run_enrichment_job(
                    file_id=89,
                    file_path=self.sql_file_path,
                    vault_id=1,
                    file_hash="abcdef012345",
                    chunks=[chunk],
                    document_text="indexed text",
                )
            )
        finally:
            settings.chunk_enrichment_enabled = original_enabled
            settings.reupload_safe_order = original_safe_order

        self.assertEqual(vector_store.old_ids, ["89_abcdef01_default_0"])
        self.assertEqual(vector_store.records[0]["id"], "89_abcdef01_default_0__enriched")
        self.assertIn("Summary: fresh enrichment", vector_store.records[0]["text"])

    def test_candidate_enrichment_skips_multiscale_duplicates_by_parent_window(self):
        """Only one representative per parent window is enriched for multi-scale chunks."""
        chunks = [
            ProcessedChunk(
                text="small window",
                metadata={"chunk_scale": "768", "chunk_index": "768_0"},
                chunk_index=0,
                parent_window_start=0,
                parent_window_end=100,
            ),
            ProcessedChunk(
                text="large same window",
                metadata={"chunk_scale": "1536", "chunk_index": "1536_0"},
                chunk_index=0,
                parent_window_start=0,
                parent_window_end=100,
            ),
            ProcessedChunk(
                text="second window",
                metadata={"chunk_scale": "768", "chunk_index": "768_1"},
                chunk_index=1,
                parent_window_start=100,
                parent_window_end=200,
            ),
        ]

        candidates = self.processor._candidate_chunks_for_enrichment(42, chunks)

        self.assertEqual(len(candidates), 2)
        self.assertEqual([chunk.text for chunk, _ in candidates], ["small window", "second window"])

    def test_candidate_enrichment_keeps_nondefault_chunks_without_parent_windows(self):
        """Non-default chunks without parent offsets must not dedupe by text prefix alone."""
        shared_prefix = "same prefix " * 60
        chunks = [
            ProcessedChunk(
                text=shared_prefix + "alpha",
                metadata={"chunk_scale": "768", "chunk_index": "768_0"},
                chunk_index=0,
            ),
            ProcessedChunk(
                text=shared_prefix + "beta",
                metadata={"chunk_scale": "1536", "chunk_index": "1536_0"},
                chunk_index=0,
            ),
        ]

        candidates = self.processor._candidate_chunks_for_enrichment(42, chunks)

        self.assertEqual(len(candidates), 2)
        self.assertEqual([chunk.text for chunk, _ in candidates], [chunks[0].text, chunks[1].text])


class TestSpreadsheetAdaptiveChunking(unittest.TestCase):
    """Test cases for adaptive spreadsheet chunking."""

    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up temp files."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_wide_spreadsheet_chunks_respect_max_size(self):
        """
        Test that wide spreadsheets (100+ columns) produce chunks
        under the 8192 char embedding limit.
        """
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not available")

        # Create a wide spreadsheet: 100 columns × 50 rows
        # Each cell has ~20 chars, so estimated chunk size would be huge
        # without adaptive chunking
        num_cols = 100
        num_rows = 50
        data = {
            f"col_{i}": [f"value_{i}_{j}" for j in range(num_rows)]
            for i in range(num_cols)
        }
        df = pd.DataFrame(data)

        csv_path = os.path.join(self.temp_dir, "wide_sheet.csv")
        df.to_csv(csv_path, index=False)

        from app.services.document_processor import SpreadsheetParser
        parser = SpreadsheetParser()
        chunks = parser.parse(csv_path)

        # Verify chunks were produced
        self.assertGreater(len(chunks), 0, "Expected at least one chunk")

        # Verify all chunks are under the max size
        max_chunk_chars = parser.MAX_CHUNK_CHARS
        for i, chunk in enumerate(chunks):
            chunk_size = len(chunk["text"])
            self.assertLessEqual(
                chunk_size,
                max_chunk_chars,
                f"Chunk {i} exceeds max size: {chunk_size} > {max_chunk_chars}",
            )

        # Verify adaptive chunking reduced row count from 50
        # (With 100 columns, 50 rows would be ~20000+ chars)
        # If chunks respect max, row count must have been reduced
        total_rows = sum(
            chunk["metadata"]["row_end"] - chunk["metadata"]["row_start"] + 1
            for chunk in chunks
        )
        self.assertEqual(
            total_rows,
            num_rows,
            f"Expected {num_rows} total rows across chunks, got {total_rows}",
        )

    def test_narrow_spreadsheet_uses_default_rows_per_chunk(self):
        """
        Test that narrow spreadsheets (few columns) still use the default
        ROWS_PER_CHUNK value for efficiency.
        """
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not available")

        # Create a narrow spreadsheet: 5 columns × 100 rows
        # This should fit many rows per chunk
        num_cols = 5
        num_rows = 100
        data = {
            f"col_{i}": [f"val_{i}_{j}" for j in range(num_rows)]
            for i in range(num_cols)
        }
        df = pd.DataFrame(data)

        csv_path = os.path.join(self.temp_dir, "narrow_sheet.csv")
        df.to_csv(csv_path, index=False)

        from app.services.document_processor import SpreadsheetParser
        parser = SpreadsheetParser()
        chunks = parser.parse(csv_path)

        # Verify chunks were produced
        self.assertGreater(len(chunks), 0, "Expected at least one chunk")

        # With narrow columns, should have fewer chunks (more rows per chunk)
        # 100 rows / 50 rows_per_chunk = 2 chunks (approximately)
        # Due to header overhead and filtering, might be 2-3 chunks
        self.assertLessEqual(
            len(chunks),
            3,
            f"Narrow spreadsheet should have ~2 chunks, got {len(chunks)}",
        )

        # Verify all chunks respect max size
        for i, chunk in enumerate(chunks):
            chunk_size = len(chunk["text"])
            self.assertLessEqual(
                chunk_size,
                parser.MAX_CHUNK_CHARS,
                f"Chunk {i} exceeds max size: {chunk_size}",
            )

    def test_single_row_with_long_cell_values_no_data_loss(self):
        """
        Test that a single row with extremely long cell values is split
        into multiple column-group chunks without data loss.
        """
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not available")

        # Create a spreadsheet with 1 row and 10 columns, each with a 1000-char value
        # Total row would be ~15,000 chars (exceeds 8192)
        num_cols = 10
        long_val = "x" * 1000
        data = {f"col_{i}": [long_val] for i in range(num_cols)}
        df = pd.DataFrame(data)

        csv_path = os.path.join(self.temp_dir, "long_cells.csv")
        df.to_csv(csv_path, index=False)

        from app.services.document_processor import SpreadsheetParser
        parser = SpreadsheetParser()
        chunks = parser.parse(csv_path)

        # Should produce multiple column-group chunks
        self.assertGreater(
            len(chunks),
            1,
            "Long cell values should trigger column-group splitting",
        )

        # Verify all chunks respect max size
        for i, chunk in enumerate(chunks):
            chunk_size = len(chunk["text"])
            self.assertLessEqual(
                chunk_size,
                parser.MAX_CHUNK_CHARS,
                f"Chunk {i} exceeds max size: {chunk_size}",
            )

        # Verify no data loss: all column names should appear in at least one chunk
        all_chunk_text = " ".join(chunk["text"] for chunk in chunks)
        for col_idx in range(num_cols):
            col_name = f"col_{col_idx}"
            self.assertIn(
                col_name,
                all_chunk_text,
                f"Column '{col_name}' missing from all chunks",
            )

        # Verify column-group metadata is present (proves column splitting happened)
        col_group_chunks = [c for c in chunks if c["metadata"].get("col_group") is not None]
        self.assertGreaterEqual(
            len(col_group_chunks),
            1,
            "Column-group chunks not found; splitting may not have occurred",
        )

    def test_mixed_row_sizes_with_column_splitting(self):
        """
        Test a spreadsheet where some rows are normal and one row is very wide,
        triggering column splitting only for the wide row.
        """
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not available")

        # Create a spreadsheet:
        # - Row 0: 50 columns with short values (fits in one chunk)
        # - Row 1: 50 columns with 500-char values each (will trigger column splitting)
        # - Row 2: 50 columns with short values (fits in one chunk)
        num_cols = 50
        short_val = "s"
        long_val = "x" * 500

        data = {
            f"col_{i}": [short_val, long_val, short_val] for i in range(num_cols)
        }
        df = pd.DataFrame(data)

        csv_path = os.path.join(self.temp_dir, "mixed_rows.csv")
        df.to_csv(csv_path, index=False)

        from app.services.document_processor import SpreadsheetParser
        parser = SpreadsheetParser()
        chunks = parser.parse(csv_path)

        # Should produce multiple chunks due to row 1 column splitting
        self.assertGreater(len(chunks), 1, "Expected multiple chunks due to column splitting")

        # Verify all chunks respect max size
        for i, chunk in enumerate(chunks):
            chunk_size = len(chunk["text"])
            self.assertLessEqual(
                chunk_size,
                parser.MAX_CHUNK_CHARS,
                f"Chunk {i} exceeds max size: {chunk_size}",
            )

    def test_non_uniform_column_sizes_triggers_validation(self):
        """
        Test that non-uniform column sizes (some moderate, some huge) correctly
        trigger the validation check that prevents oversized columns from being
        flushed as single-column chunks.

        This test specifically catches the bug where a column that fits alone
        (1035 chars) is followed by one that exceeds alone (8193 chars).
        """
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not available")

        # Create a row with:
        # - col_0 to col_4: moderate values that fit together
        # - col_5: huge value that exceeds 8192 when rendered alone
        data = {
            "col_0": ["moderate_0" * 50],  # ~500 chars
            "col_1": ["moderate_1" * 50],  # ~500 chars
            "col_2": ["moderate_2" * 50],  # ~500 chars
            "col_3": ["moderate_3" * 50],  # ~500 chars
            "col_4": ["moderate_4" * 50],  # ~500 chars
            "col_5": ["huge" * 2500],  # ~10,000 chars (exceeds 8192 alone)
        }
        df = pd.DataFrame(data)

        csv_path = os.path.join(self.temp_dir, "non_uniform_cols.csv")
        df.to_csv(csv_path, index=False)

        from app.services.document_processor import SpreadsheetParser
        parser = SpreadsheetParser()
        chunks = parser.parse(csv_path)

        # Should produce multiple chunks (normal cols + split huge col)
        self.assertGreater(len(chunks), 1, "Expected multiple chunks")

        # Verify ALL chunks respect max size (the critical check)
        for i, chunk in enumerate(chunks):
            chunk_size = len(chunk["text"])
            self.assertLessEqual(
                chunk_size,
                parser.MAX_CHUNK_CHARS,
                f"Chunk {i} EXCEEDS max size: {chunk_size} > {parser.MAX_CHUNK_CHARS}. "
                f"This indicates the validation check failed.",
            )

        # Verify column data is present (no complete loss)
        all_text = " ".join(c["text"] for c in chunks)
        self.assertIn("col_0", all_text, "col_0 missing")
        self.assertIn("col_5", all_text, "col_5 missing")
        self.assertIn("huge", all_text, "col_5 value data missing — may have been dropped instead of truncated")

    def test_single_column_overflow_truncation(self):
        """
        Test that a single column with a value exceeding MAX_CHUNK_CHARS is
        truncated rather than dropped. Exercises lines 224-244 (the truncation
        path). Requires a value >= 8156 chars to trigger it.
        """
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not available")

        long_val = "overflow" * 1125  # 9000 chars, exceeds 8192 alone
        data = {"col_overflow": [long_val]}
        df = pd.DataFrame(data)

        csv_path = os.path.join(self.temp_dir, "single_col_overflow.csv")
        df.to_csv(csv_path, index=False)

        from app.services.document_processor import SpreadsheetParser
        parser = SpreadsheetParser()
        chunks = parser.parse(csv_path)

        self.assertEqual(len(chunks), 1, "Expected exactly 1 chunk for single-column overflow")

        chunk = chunks[0]
        self.assertLessEqual(
            len(chunk["text"]),
            parser.MAX_CHUNK_CHARS,
            f"Chunk size {len(chunk['text'])} exceeds MAX_CHUNK_CHARS {parser.MAX_CHUNK_CHARS}",
        )
        self.assertIn("col_overflow", chunk["text"], "Column name missing from truncated chunk")
        self.assertIn("overflow", chunk["text"], "Cell data missing — value may have been dropped instead of truncated")
        self.assertIsNotNone(
            chunk["metadata"].get("col_group"),
            "col_group metadata missing — column-split path was not taken",
        )


if __name__ == '__main__':
    unittest.main()
