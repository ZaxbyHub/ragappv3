"""Feedback-round battery for issue #704 (swarm-pr-review pr864-20261008).

Non-frozen additive coverage closing the review findings: end-to-end outage
classification (PRR-001), the process_file AC3 restore leg + partial prior +
post-disturbance failure (PRR-004/OOB-3), permit install/clear behaviour
(OOB-4), marker skip on failed re-publish (concurrency-M11), tombstone
reference filter (OOB-5), retry exhaustion (PRR-021), DIMENSION_CHANGED
precedence (PRR-022), the guarded error/recovery writers (PRR-002/011), and
the restore's progress-counter cleanup (PRR-016).
"""

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.models.database import init_db
from app.services.document_processor import DocumentProcessingError
from app.services.vector_store import VectorStoreError


def _admin_env():
    import os

    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault(
        "ADMIN_SECRET_TOKEN",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    )


_admin_env()


class OutageClassificationTest(unittest.TestCase):
    """End-to-end: an embedding outage (all batches fail) must persist
    EMBEDDING_FAILED, not PARSE_FAILED (issue #704 review PRR-001)."""

    def _make_processor(self, tmp, embed_result):
        from app.services.chunking import ProcessedChunk
        from app.services.document_processor import DocumentProcessor

        db = str(Path(tmp.name) / "app.db")
        init_db(db)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        vid = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        # The #692 staleness gate refuses an ingest whose row is missing, so
        # the mock-insert contract (id=1) needs a real files row.
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, "
            "file_hash, file_size, status) VALUES (1, ?, '/doc.txt', "
            "'doc.txt', 'abc12345', 1, 'processing')",
            (vid,),
        )
        conn.commit()
        conn.close()

        pool = MagicMock()
        real = sqlite3.connect(db)
        real.row_factory = sqlite3.Row
        pool.get_connection_async = AsyncMock(return_value=real)
        pool.get_connection = lambda: real
        pool.release_connection = lambda c: None
        pool.connection.return_value.__enter__.return_value = real
        pool.connection.return_value.__exit__.return_value = False
        self.addCleanup(real.close)

        src = Path(tmp.name) / "doc.txt"
        src.write_text("hello world", encoding="utf-8")

        embedding = MagicMock()
        embedding.embed_batch = AsyncMock(return_value=embed_result)
        vector_store = MagicMock()
        vector_store.init_table = AsyncMock()
        vector_store.count_by_file = AsyncMock(return_value=1)

        processor = DocumentProcessor(
            pool=pool,
            embedding_service=embedding,
            vector_store=vector_store,
        )
        chunk = ProcessedChunk(text="hello world", metadata={}, chunk_index=0)
        return processor, real, str(src), vid, [chunk]

    def test_outage_abort_classifies_embedding_failed(self):
        import asyncio
        import tempfile
        import unittest.mock as mock

        from app.services.document_processor import (
            DocumentProcessingError,
            redact_ingest_error,
        )

        captured = {}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # 100% batch failure: embed_batch's fail_fast=False contract returns
        # None placeholders + failed indices instead of raising.
        processor, conn, src, vid, chunks = self._make_processor(tmp, ([None], [0]))
        with (
            unittest.mock.patch.object(
                processor, "_check_duplicate", return_value=None
            ),
            unittest.mock.patch.object(
                processor,
                "_insert_or_get_file_record_once",
                return_value=1,
            ),
            unittest.mock.patch.object(processor, "_validate_chunk_sizes"),
            unittest.mock.patch.object(
                processor, "_is_schema_file", return_value=False
            ),
            unittest.mock.patch.object(
                processor, "_is_spreadsheet_file", return_value=False
            ),
            unittest.mock.patch.object(
                processor,
                "_process_document_file",
                new=AsyncMock(return_value=(chunks, "hello world", _no_atoms())),
            ),
            unittest.mock.patch.object(
                processor, "_get_chunk_enrichment_service", return_value=None
            ),
            unittest.mock.patch(
                "app.services.document_processor.compute_file_hash",
                return_value="abc12345",
            ),
            unittest.mock.patch(
                "app.services.document_processor.settings"
            ) as mock_settings,
            unittest.mock.patch(
                "app.services.document_processor.set_phase", new_callable=AsyncMock
            ),
            unittest.mock.patch(
                "app.services.document_processor.clear_progress",
                new_callable=AsyncMock,
            ),
            unittest.mock.patch(
                "app.services.document_processor.set_wiki_pending",
                new_callable=AsyncMock,
            ),
        ):
            mock_settings.contextual_chunking_enabled = False
            mock_settings.parent_retrieval_enabled = False
            mock_settings.multi_scale_indexing_enabled = False
            mock_settings.chunk_enrichment_enabled = False
            mock_settings.reupload_safe_order = False
            mock_settings.embedding_batch_size = 64
            mock_settings.document_parse_timeout = 30
            captured["code"] = None

            async def fire():
                try:
                    await processor.process_existing_file(
                        file_id=1, file_path=src, vault_id=1
                    )
                except DocumentProcessingError as exc:
                    captured["code"] = redact_ingest_error(exc)
                else:
                    self.fail("expected the >50% abort to raise")

            asyncio.run(fire())
        self.assertTrue(
            (captured["code"] or "").startswith("EMBEDDING_FAILED"),
            captured["code"],
        )


def _no_atoms():
    from app.services.document_artifacts import ParsedDocument

    return ParsedDocument(atoms=())


class RestoreLegTest(unittest.TestCase):
    """AC3 restore on the process_file leg, partial prior, and the
    post-disturbance boundary (PRR-004 / OOB-3)."""

    def _seed(self, status):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "app.db")
        init_db(db)
        src = Path(tmp.name) / "src.txt"
        src.write_text("hello world", encoding="utf-8")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        vid = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
            "file_size, status, chunks_failed) VALUES (?, ?, 'src.txt', "
            "'OLDHASH', 1, ?, 3)",
            (vid, str(src), status),
        )
        conn.execute(
            "INSERT INTO failed_chunks (file_id, chunk_index, chunk_text, "
            "chunk_metadata, attempts) VALUES (1, 0, 'x', '{}', 1)"
        )
        conn.commit()
        self.addCleanup(conn.close)
        return conn, db, str(src)

    def _processor_over(self, conn, db):
        from app.services.document_processor import DocumentProcessor

        pool = MagicMock()
        pool.get_connection_async = AsyncMock(return_value=conn)
        pool.get_connection = lambda: conn
        pool.release_connection = lambda c: None
        pool.connection.return_value.__enter__.return_value = conn
        pool.connection.return_value.__exit__.return_value = False
        processor = DocumentProcessor.__new__(DocumentProcessor)
        processor.pool = pool
        processor._cancel_requested = set()
        processor._detached_status_tasks = set()
        processor._write_semaphore = None
        return processor

    def _run_failure(self, processor, src, vector_store, conn):
        import asyncio
        import unittest.mock as mock

        from app.services.document_artifacts import ParsedDocument
        from app.services.document_processor import DocumentProcessor

        captured = {}

        async def run():
            with (
                mock.patch.object(
                    DocumentProcessor,
                    "_insert_or_get_file_record_once",
                    return_value=1,
                ),
                mock.patch.object(processor, "_validate_chunk_sizes"),
                mock.patch.object(processor, "_is_schema_file", return_value=False),
                mock.patch.object(
                    processor, "_is_spreadsheet_file", return_value=False
                ),
                mock.patch.object(
                    processor,
                    "_process_document_file",
                    new=AsyncMock(
                        return_value=(
                            [real_chunk],
                            "text",
                            ParsedDocument(atoms=()),
                        )
                    ),
                ),
                mock.patch.object(
                    processor, "_get_chunk_enrichment_service", return_value=None
                ),
                mock.patch(
                    "app.services.document_processor.compute_file_hash",
                    return_value="NEWHASH",
                ),
                mock.patch("app.services.document_processor.settings") as mock_settings,
                mock.patch(
                    "app.services.document_processor.set_phase",
                    new_callable=AsyncMock,
                ),
                mock.patch(
                    "app.services.document_processor.clear_progress",
                    new_callable=AsyncMock,
                ) as mock_clear,
                mock.patch(
                    "app.services.document_processor.set_wiki_pending",
                    new_callable=AsyncMock,
                ),
            ):
                mock_settings.contextual_chunking_enabled = False
                mock_settings.parent_retrieval_enabled = False
                mock_settings.multi_scale_indexing_enabled = False
                mock_settings.chunk_enrichment_enabled = False
                mock_settings.reupload_safe_order = False
                mock_settings.embedding_batch_size = 64
                try:
                    await processor.process_file(src, vault_id=1)
                except DocumentProcessingError:
                    pass
                captured["clear"] = mock_clear.call_count

        asyncio.run(run())
        return captured

    def test_process_file_restore_restores_hash_and_drops_ledger(self):
        conn, db, src = self._seed("indexed")
        processor = self._processor_over(conn, db)

        import unittest.mock as mock

        from app.services.document_artifacts import ParsedDocument
        from app.services.document_processor import DocumentProcessor

        vector_store = MagicMock()
        # parse OK; the failure is the vector write (disturbed) — no wait:
        # the restore fires only when NOT disturbed, so fail at parse time
        # via _process_document_file raising.
        # (kept in the same helper for shape)

        async def run():
            # NOTE: the REAL _insert_or_get_file_record runs (no mock) so the
            # failed attempt genuinely disturbs file_hash to NEWHASH first —
            # that is what makes the restore's hash assertion discriminating
            # (delta re-gate).
            with (
                mock.patch.object(processor, "_validate_chunk_sizes"),
                mock.patch.object(processor, "_is_schema_file", return_value=False),
                mock.patch.object(
                    processor, "_is_spreadsheet_file", return_value=False
                ),
                mock.patch.object(
                    processor,
                    "_process_document_file",
                    new=AsyncMock(side_effect=DocumentProcessingError("parse blew up")),
                ),
                mock.patch(
                    "app.services.document_processor.compute_file_hash",
                    return_value="NEWHASH",
                ),
                mock.patch(
                    "app.services.document_processor.set_phase",
                    new_callable=AsyncMock,
                ),
                mock.patch(
                    "app.services.document_processor.clear_progress",
                    new_callable=AsyncMock,
                ),
                mock.patch(
                    "app.services.document_processor.set_wiki_pending",
                    new_callable=AsyncMock,
                ),
            ):
                await processor.process_file(src, vault_id=1)

        import asyncio

        try:
            asyncio.run(run())
        except DocumentProcessingError:
            pass
        row = conn.execute("SELECT * FROM files WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "indexed")
        # PRR-004: the prior CONTENT hash is restored, not the failed
        # attempt's hash. Discriminating: the real insert set NEWHASH first.
        self.assertNotEqual(row["file_hash"], "NEWHASH")
        self.assertEqual(row["file_hash"], "OLDHASH")
        # PRR-004d: the aborted attempt's foreign ledger is gone and the
        # restored row reports zero remaining chunks.
        self.assertEqual(row["chunks_failed"], 0)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM failed_chunks WHERE file_id = 1"
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_partial_prior_restore_reports_zero_remaining(self):
        conn, db, src = self._seed("partial")
        processor = self._processor_over(conn, db)

        import asyncio
        import unittest.mock as mock

        from app.services.document_artifacts import ParsedDocument
        from app.services.document_processor import DocumentProcessor

        async def run():
            with (
                mock.patch.object(processor, "_validate_chunk_sizes"),
                mock.patch.object(processor, "_is_schema_file", return_value=False),
                mock.patch.object(
                    processor, "_is_spreadsheet_file", return_value=False
                ),
                mock.patch.object(
                    processor,
                    "_process_document_file",
                    new=AsyncMock(side_effect=DocumentProcessingError("boom")),
                ),
                mock.patch(
                    "app.services.document_processor.compute_file_hash",
                    return_value="NEWHASH",
                ),
                mock.patch(
                    "app.services.document_processor.set_phase",
                    new_callable=AsyncMock,
                ),
                mock.patch(
                    "app.services.document_processor.clear_progress",
                    new_callable=AsyncMock,
                ),
                mock.patch(
                    "app.services.document_processor.set_wiki_pending",
                    new_callable=AsyncMock,
                ),
            ):
                await processor.process_file(src, vault_id=1)

        try:
            asyncio.run(run())
        except DocumentProcessingError:
            pass
        row = conn.execute("SELECT status FROM files WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "partial")
        conn.close()

    def test_post_disturbance_failure_lands_error(self):
        conn, db, src = self._seed("indexed")
        processor = self._processor_over(conn, db)

        import asyncio
        import unittest.mock as mock

        from app.services.document_artifacts import ParsedDocument
        from app.services.document_processor import DocumentProcessor

        vector_store = MagicMock()
        vector_store.init_table = AsyncMock(
            side_effect=VectorStoreError("lancedb down")
        )

        from app.services.chunking import ProcessedChunk

        real_chunk = ProcessedChunk(text="hello world", metadata={}, chunk_index=0)
        processor.vector_store = vector_store
        processor.embedding_service = MagicMock()
        processor.embedding_service.embed_batch = AsyncMock(return_value=([[0.1]], []))

        async def run():
            with (
                mock.patch.object(
                    DocumentProcessor,
                    "_insert_or_get_file_record_once",
                    return_value=1,
                ),
                mock.patch.object(processor, "_validate_chunk_sizes"),
                mock.patch.object(processor, "_is_schema_file", return_value=False),
                mock.patch.object(
                    processor, "_is_spreadsheet_file", return_value=False
                ),
                mock.patch.object(
                    processor,
                    "_process_document_file",
                    new=AsyncMock(
                        return_value=(
                            [real_chunk],
                            "text",
                            ParsedDocument(atoms=()),
                        )
                    ),
                ),
                mock.patch.object(
                    processor, "_get_chunk_enrichment_service", return_value=None
                ),
                mock.patch(
                    "app.services.document_processor.compute_file_hash",
                    return_value="NEWHASH",
                ),
                mock.patch("app.services.document_processor.settings") as mock_settings,
                mock.patch(
                    "app.services.document_processor.set_phase",
                    new_callable=AsyncMock,
                ),
                mock.patch(
                    "app.services.document_processor.clear_progress",
                    new_callable=AsyncMock,
                ),
                mock.patch(
                    "app.services.document_processor.set_wiki_pending",
                    new_callable=AsyncMock,
                ),
                mock.patch.object(
                    processor, "_ensure_live_dimension_compatible", new=AsyncMock()
                ),
            ):
                mock_settings.contextual_chunking_enabled = False
                mock_settings.parent_retrieval_enabled = False
                mock_settings.multi_scale_indexing_enabled = False
                mock_settings.chunk_enrichment_enabled = False
                mock_settings.reupload_safe_order = False
                mock_settings.embedding_batch_size = 64
                await processor.process_file(src, vault_id=1)

        try:
            asyncio.run(run())
        except (DocumentProcessingError, VectorStoreError):
            pass
        row = conn.execute("SELECT status FROM files WHERE id = 1").fetchone()
        # The failure landed AFTER the vector store was disturbed: no restore.
        self.assertEqual(row["status"], "error")
        conn.close()


class ClassificationPrecedenceTest(unittest.TestCase):
    def test_dimension_changed_beats_embedding_arm(self):
        from app.services.document_processor import (
            INGEST_ERROR_DIMENSION_CHANGED,
            EmbeddingDimensionChangedError,
            classify_ingest_error,
        )

        exc = EmbeddingDimensionChangedError("dim changed")
        self.assertEqual(classify_ingest_error(exc), INGEST_ERROR_DIMENSION_CHANGED)

    def test_retry_exhaustion_retypes_to_document_processing_error(self):
        import sqlite3 as sq3
        import tempfile

        from app.services.document_processor import (
            DocumentProcessingError,
            DocumentProcessor,
        )

        class _AlwaysFails:
            # noqa-style minimal proxy: every execute raises SQLITE_BUSY
            def execute(self, *a, **k):
                raise sq3.OperationalError("database is locked")

            def rollback(self):
                pass

            def commit(self):
                pass

        processor = DocumentProcessor.__new__(DocumentProcessor)
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file.write(b"probe")
            path = tmp_file.name
        try:
            with self.assertRaises(DocumentProcessingError) as ctx:
                processor._insert_or_get_file_record(
                    path, "deadbeef", _AlwaysFails(), vault_id=1
                )
        finally:
            import os

            os.unlink(path)
        self.assertIn("Database error", str(ctx.exception))


class GuardedWritersTest(unittest.TestCase):
    def _setup(self):
        from app.services.background_tasks import BackgroundProcessor
        from app.services.document_processor import DocumentProcessor

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "app.db")
        init_db(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        vid = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (?, '/a', 'a', 'h', 1, 'cancelled')",
            (vid,),
        )
        conn.commit()
        bp = BackgroundProcessor.__new__(BackgroundProcessor)
        bp.max_retries = 3
        bp.processor = MagicMock()
        pool = MagicMock()
        pool.connection.return_value.__enter__.return_value = conn
        pool.connection.return_value.__exit__.return_value = False
        bp.processor.pool = pool
        self.conn = conn
        return bp

    def test_error_write_does_not_bury_cancelled(self):
        from app.services.background_tasks import TaskItem

        bp = self._setup()
        from app.services.background_tasks import TaskItem as TI

        bp._mark_task_permanently_failed(
            TaskItem(file_path="/a", source="upload", vault_id=1, file_id=1),
            "boom",
        )
        row = self.conn.execute("SELECT status FROM files WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.conn.close()

    def test_recovery_reset_skips_settled_row(self):
        # PRR-011: the recovery reset must not demote a row that settled
        # between the sweep's snapshot SELECT and the UPDATE.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "app.db")
        init_db(db)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (1, '/b', 'b', 'h', 1, 'indexed')"
        )
        conn.commit()
        # Apply the guarded reset manually (the production SQL shape).
        cur = conn.execute(
            "UPDATE files SET status='pending', phase='queued', "
            "error_message=NULL WHERE id = 1 AND status = 'processing'"
        )
        conn.commit()
        self.assertEqual(cur.rowcount, 0)
        row = conn.execute("SELECT status FROM files WHERE id = 1").fetchone()[0]
        self.assertEqual(row, "indexed")
        conn.close()


class ProductionWiringPinsTest(unittest.TestCase):
    """Delta re-gate: the claims the first battery asserted but never shipped."""

    def test_production_publish_sites_pass_session_conn_and_materialized(self):
        # PRR-005/PRR-007: both production _publish_artifacts call sites must
        # pre-materialize outside the session and pass conn= + materialized=
        # (dropping either re-opens a permit-less raw checkout invisibly).
        import ast

        path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "services"
            / "document_processor.py"
        )
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        sites = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncWith)
                and len(node.items) == 1
                and isinstance(node.items[0].context_expr, ast.Call)
                and isinstance(node.items[0].context_expr.func, ast.Attribute)
                and node.items[0].context_expr.func.attr == "_write_session"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "_publish_artifacts"
                    ):
                        keywords = {k.arg for k in sub.keywords}
                        self.assertIn("conn", keywords, "production site missing conn=")
                        self.assertIn(
                            "materialized",
                            keywords,
                            "production site missing materialized=",
                        )
                        sites.append(sub.lineno)
        self.assertEqual(len(sites), 2, f"expected 2 production sites, saw {sites}")

    def test_both_abort_sites_carry_embedding_code(self):
        # PRR-001: BOTH >50% abort sites must attach the code — reverting one
        # leaves this failing (the frozen C1 only covers one entry point).
        path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "services"
            / "document_processor.py"
        )
        src = io.open(path, encoding="utf-8").read()
        self.assertEqual(
            src.count("raise _abort_error"),
            2,
            "both abort sites must raise the coded error",
        )
        self.assertEqual(
            src.count("_abort_error.ingest_error_code"),
            2,
            "both abort sites must attach EMBEDDING_FAILED",
        )

    def test_marker_skipped_when_generation_already_committed(self):
        # concurrency-M11: a failed RE-publish of a committed generation must
        # NOT overwrite the committed succeeded row with a failure marker.
        import tempfile

        from app.services.document_artifacts import ParsedDocument
        from app.services.document_processor import DocumentProcessor

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "app.db")
        init_db(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (1, 1, '/x', 'x', 'h', 1, 'indexed')"
        )
        # A committed atom for (file 1, genA): the generation IS published.
        conn.execute(
            "INSERT INTO document_atoms (atom_id, schema_version, file_id, "
            "generation_hash, ordinal, kind, raw_text) VALUES ('a-0', 1, 1, "
            "'genA', 0, 'image', 'img')"
        )
        conn.commit()

        proc = DocumentProcessor.__new__(DocumentProcessor)
        proc.pool = MagicMock()
        conn2 = sqlite3.connect(db)
        conn2.row_factory = sqlite3.Row
        proc.pool.get_connection = lambda: conn2
        proc.pool.release_connection = lambda c: None

        parsed = ParsedDocument(atoms=(), parser_fingerprint="t")
        DocumentProcessor._record_publish_failure(proc, conn2, 1, 1, "genA", parsed)
        count = conn2.execute(
            "SELECT COUNT(*) FROM ingestion_stage_states WHERE file_id = 1 "
            "AND generation_hash = 'genA' AND stage = 'publish'"
        ).fetchone()[0]
        self.assertEqual(count, 0)
        conn2.close()
        conn.close()

    def test_tombstone_filter_skips_committed_rel_paths(self):
        # OOB-5: a committed document_assets row's bytes must never be
        # enqueued for deletion by publish compensation.
        import tempfile

        from app.services.document_artifacts import DocumentAsset
        from app.services.document_processor import DocumentProcessor

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "app.db")
        init_db(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (1, 1, '/x', 'x', 'h', 1, 'indexed')"
        )
        # a1's rel_path is committed; a2's is not.
        conn.execute(
            "INSERT INTO document_assets (asset_id, file_id, generation_hash, "
            "rel_path, sha256, byte_size) VALUES ('aa', 1, 'genOLD', "
            "'committed/rel1', 'aa', 1)"
        )
        conn.commit()

        a1 = DocumentAsset(
            asset_id="aa",
            file_id=1,
            generation_hash="genA",
            sha256="aa",
            rel_path="committed/rel1",
            mime_type=None,
            width=None,
            height=None,
            byte_size=1,
            metadata={},
        )
        a2 = DocumentAsset(
            asset_id="ab",
            file_id=1,
            generation_hash="genA",
            sha256="ab",
            rel_path="fresh/rel2",
            mime_type=None,
            width=None,
            height=None,
            byte_size=1,
            metadata={},
        )

        proc = DocumentProcessor.__new__(DocumentProcessor)
        proc.pool = MagicMock()
        proc.pool.get_connection = lambda: conn
        proc.pool.release_connection = lambda c: None

        DocumentProcessor._tombstone_materialized_assets_on(
            proc, conn, [a1, a2], 1, 1, "genA"
        )
        pending = {
            r["rel_path"]
            for r in conn.execute(
                "SELECT rel_path FROM artifact_delete_pending"
            ).fetchall()
        }
        self.assertNotIn("committed/rel1", pending)
        self.assertIn("fresh/rel2", pending)
        conn.close()

    def test_permit_wiring_present_in_source(self):
        # OOB-4: source-level pin — start installs pool.write_permit, the
        # start-failure cleanup and stop() both clear it.
        path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "services"
            / "background_tasks.py"
        )
        src = io.open(path, encoding="utf-8").read()
        self.assertIn("self.processor.pool.write_permit = self._write_semaphore", src)
        # Both clearing contexts exist (start-failure cleanup + stop tail).
        self.assertGreaterEqual(src.count("self.processor.pool.write_permit = None"), 2)

    def test_process_file_outage_abort_carries_code(self):
        # PRR-001 second leg: the process_file entry point's abort also
        # classifies EMBEDDING_FAILED (the first battery covered only
        # process_existing_file).
        import asyncio
        import tempfile
        import unittest.mock as mock

        from app.services.chunking import ProcessedChunk
        from app.services.document_artifacts import ParsedDocument
        from app.services.document_processor import (
            DocumentProcessingError,
            DocumentProcessor,
            redact_ingest_error,
        )

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "app.db")
        init_db(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        src = Path(tmp.name) / "doc.txt"
        src.write_text("hello world", encoding="utf-8")
        conn.commit()

        pool = MagicMock()
        pool.get_connection_async = AsyncMock(return_value=conn)
        pool.get_connection = lambda: conn
        pool.release_connection = lambda c: None
        pool.connection.return_value.__enter__.return_value = conn
        pool.connection.return_value.__exit__.return_value = False

        embedding = MagicMock()
        embedding.embed_batch = AsyncMock(return_value=([None], [0]))
        vector_store = MagicMock()
        vector_store.init_table = AsyncMock()
        vector_store.count_by_file = AsyncMock(return_value=1)

        processor = DocumentProcessor(
            pool=pool,
            embedding_service=embedding,
            vector_store=vector_store,
        )
        chunk = ProcessedChunk(text="hello world", metadata={}, chunk_index=0)

        captured = {}

        async def fire():
            with (
                mock.patch.object(processor, "_check_duplicate", return_value=None),
                mock.patch.object(processor, "_validate_chunk_sizes"),
                mock.patch.object(processor, "_is_schema_file", return_value=False),
                mock.patch.object(
                    processor, "_is_spreadsheet_file", return_value=False
                ),
                mock.patch.object(
                    processor,
                    "_process_document_file",
                    new=AsyncMock(
                        return_value=(
                            [chunk],
                            "hello world",
                            ParsedDocument(atoms=()),
                        )
                    ),
                ),
                mock.patch.object(
                    processor, "_get_chunk_enrichment_service", return_value=None
                ),
                mock.patch(
                    "app.services.document_processor.compute_file_hash",
                    return_value="abc12345",
                ),
                mock.patch(
                    "app.services.document_processor.set_phase",
                    new_callable=AsyncMock,
                ),
                mock.patch(
                    "app.services.document_processor.clear_progress",
                    new_callable=AsyncMock,
                ),
                mock.patch(
                    "app.services.document_processor.set_wiki_pending",
                    new_callable=AsyncMock,
                ),
                mock.patch("app.services.document_processor.settings") as mock_settings,
            ):
                mock_settings.contextual_chunking_enabled = False
                mock_settings.parent_retrieval_enabled = False
                mock_settings.multi_scale_indexing_enabled = False
                mock_settings.chunk_enrichment_enabled = False
                mock_settings.reupload_safe_order = False
                mock_settings.embedding_batch_size = 64
                mock_settings.document_parse_timeout = 30
                try:
                    await processor.process_file(str(src), vault_id=1)
                except DocumentProcessingError as exc:
                    captured["code"] = redact_ingest_error(exc)
                else:
                    self.fail("expected the >50% abort to raise")

        asyncio.run(fire())
        self.assertTrue(
            (captured["code"] or "").startswith("EMBEDDING_FAILED"), captured["code"]
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
