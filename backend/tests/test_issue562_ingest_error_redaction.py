"""Regression tests for issue #562 (audit finding C26).

A failed ingestion must persist a stable user-facing error code plus a short
operator-safe message — never the server-absolute path or the raw exception
text — while the raw exception stays in the server log. The documents API must
also stop echoing the stored server-absolute ``file_path``.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Stub heavy optional dependencies the way the rest of the suite does.
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _db_pool import SimpleConnectionPool  # noqa: E402

from app.api.routes.documents import _vault_relative_file_path  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import SQLiteConnectionPool, init_db  # noqa: E402
from app.services.document_processor import (  # noqa: E402
    INGEST_ERROR_ENRICHMENT_FAILED,
    INGEST_ERROR_FILE_MISSING,
    INGEST_ERROR_PARSE_FAILED,
    INGEST_ERROR_PARSER_UNAVAILABLE,
    DocumentParseError,
    DocumentProcessor,
    format_ingest_error,
    redact_ingest_error,
)

_PARTITION_ERROR = ValueError("simulated corrupt document structure at offset 991")


def _failing_partition(*_args, **_kwargs):
    raise _partition_error_holder["exc"]


_partition_error_holder = {"exc": _PARTITION_ERROR}


try:
    from unstructured.partition.auto import partition as _partition  # noqa: F401
except ImportError:
    # Install the same working stub the rest of the suite uses (only when the
    # real dependency is absent); tests patch the partition attribute at test
    # scope so sibling test files are never affected.
    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = lambda *args, **kwargs: []
    unstructured = types.ModuleType("unstructured")
    unstructured.__path__ = []
    partition_mod = types.ModuleType("unstructured.partition")
    partition_mod.__path__ = []
    unstructured.partition = partition_mod
    partition_mod.auto = auto
    chunking = types.ModuleType("unstructured.chunking")
    chunking.__path__ = []
    title = types.ModuleType("unstructured.chunking.title")
    title.chunk_by_title = lambda *a, **k: []
    unstructured.chunking = chunking
    chunking.title = title
    documents = types.ModuleType("unstructured.documents")
    documents.__path__ = []
    elements = types.ModuleType("unstructured.documents.elements")
    elements.Element = type("Element", (), {})
    unstructured.documents = documents
    documents.elements = elements
    sys.modules.update(
        {
            "unstructured": unstructured,
            "unstructured.partition": partition_mod,
            "unstructured.partition.auto": auto,
            "unstructured.chunking": chunking,
            "unstructured.chunking.title": title,
            "unstructured.documents": documents,
            "unstructured.documents.elements": elements,
        }
    )


class _PartitionFailurePatch:
    """Swap in the failing partition for the duration of a test only."""

    def __init__(self, exc=None):
        self._exc = exc

    def __enter__(self):
        import unstructured.partition.auto as auto_mod

        # Save and restore the shared holder so a test that injects a
        # different exception (e.g. the parser-unavailable ImportError) does
        # not leak it into sibling tests under randomized collection order
        # (issue #565).
        self._prev_exc = _partition_error_holder["exc"]
        if self._exc is not None:
            _partition_error_holder["exc"] = self._exc
        self._auto = auto_mod
        self._original = auto_mod.partition
        auto_mod.partition = _failing_partition
        return self

    def __exit__(self, *_exc):
        self._auto.partition = self._original
        _partition_error_holder["exc"] = self._prev_exc
        return False


def _is_leak_free(value: str) -> bool:
    """A persisted message must carry no path and no raw exception text."""
    if not value:
        return False
    if "/" in value or "\\" in value:
        return False
    if value.endswith("Error") or "simulated corrupt document" in value:
        return False
    return True


class IngestErrorRedactionTest(unittest.TestCase):
    """process_file / process_existing_file persist redacted messages (#562)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="issue562-")
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (7, 'V', '')"
        )
        conn.commit()
        conn.close()
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.processor = DocumentProcessor(
            chunk_size_chars=2000, chunk_overlap_chars=200, pool=self.pool
        )
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self.temp_dir)
        self.vault_uploads = Path(self.temp_dir) / "vaults" / "7" / "uploads"
        self.vault_uploads.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        settings.data_dir = self._original_data_dir
        self.pool.close_all()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _file_row(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT file_path, status, error_message, phase, phase_message"
            " FROM files WHERE vault_id = 7"
        ).fetchone()
        conn.close()
        return row

    def _file_row_by_id(self, file_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT file_path, status, error_message, phase_message"
            " FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        conn.close()
        return row

    def test_process_file_parse_failure_persists_redacted_messages(self):
        doc = self.vault_uploads / "corrupt-probe.txt"
        doc.write_text("col1,col2\n1,2\n", encoding="utf-8")

        with _PartitionFailurePatch(), self.assertRaises(Exception):
            asyncio.run(self.processor.process_file(str(doc), vault_id=7))

        row = self._file_row()
        self.assertEqual(row["status"], "error")
        expected = format_ingest_error(INGEST_ERROR_PARSE_FAILED)
        # The persisted error fields carry only the stable code + safe reason.
        self.assertEqual(row["error_message"], expected)
        self.assertEqual(row["phase_message"], expected)
        self.assertTrue(_is_leak_free(row["error_message"]))
        self.assertTrue(_is_leak_free(row["phase_message"]))
        self.assertNotIn(str(doc), row["error_message"])
        self.assertNotIn("simulated corrupt document", row["error_message"])

    def test_process_file_parse_failure_logs_raw_exception(self):
        # The raw exception text (path + underlying error) must remain in the
        # server log at the failure site.
        doc = self.vault_uploads / "corrupt-log-probe.txt"
        doc.write_text("x", encoding="utf-8")

        with self.assertLogs("app.services.document_processor", level="ERROR") as captured:
            with _PartitionFailurePatch(), self.assertRaises(Exception):
                asyncio.run(self.processor.process_file(str(doc), vault_id=7))

        joined = "\n".join(captured.output)
        self.assertIn(str(doc), joined)
        self.assertIn("simulated corrupt document", joined)

    def test_process_existing_file_missing_file_persists_file_missing(self):
        missing = self.vault_uploads / "gone-562.txt"
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, status)"
            " VALUES (7, ?, 'gone-562.txt', 10, 'processing')",
            (str(missing),),
        )
        file_id = cur.lastrowid
        conn.commit()
        conn.close()

        with self.assertRaises(FileNotFoundError):
            asyncio.run(
                self.processor.process_existing_file(file_id, str(missing), vault_id=7)
            )

        row = self._file_row_by_id(file_id)
        self.assertEqual(row["status"], "error")
        expected = format_ingest_error(INGEST_ERROR_FILE_MISSING)
        self.assertEqual(row["error_message"], expected)
        self.assertEqual(row["phase_message"], expected)
        self.assertTrue(_is_leak_free(row["error_message"]))

    def test_process_existing_file_not_a_file_persists_file_missing(self):
        # A directory occupies the stored path: exercises the is_file branch.
        decoy_dir = self.vault_uploads / "decoy-562-dir"
        decoy_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, status)"
            " VALUES (7, ?, 'decoy-562-dir', 10, 'processing')",
            (str(decoy_dir),),
        )
        file_id = cur.lastrowid
        conn.commit()
        conn.close()

        with self.assertRaises(FileNotFoundError):
            asyncio.run(
                self.processor.process_existing_file(file_id, str(decoy_dir), vault_id=7)
            )

        row = self._file_row_by_id(file_id)
        self.assertEqual(row["status"], "error")
        self.assertEqual(
            row["error_message"], format_ingest_error(INGEST_ERROR_FILE_MISSING)
        )
        self.assertTrue(_is_leak_free(row["error_message"]))


class ParserUnavailableEndToEndTest(unittest.TestCase):
    """The missing-parser failure (ImportError wrapped by DocumentParseError)
    must surface as PARSER_UNAVAILABLE, not PARSE_FAILED (issue #562 review)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="issue562-importerror-")
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (7, 'V', '')"
        )
        conn.commit()
        conn.close()
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.processor = DocumentProcessor(
            chunk_size_chars=2000, chunk_overlap_chars=200, pool=self.pool
        )
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self.temp_dir)
        self.vault_uploads = Path(self.temp_dir) / "vaults" / "7" / "uploads"
        self.vault_uploads.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        settings.data_dir = self._original_data_dir
        self.pool.close_all()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_missing_parser_failure_persists_parser_unavailable(self):
        doc = self.vault_uploads / "no-parser.txt"
        doc.write_text("x", encoding="utf-8")

        with _PartitionFailurePatch(
            ImportError("No module named 'unstructured'")
        ), self.assertRaises(Exception):
            asyncio.run(self.processor.process_file(str(doc), vault_id=7))

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, error_message, phase_message FROM files WHERE vault_id = 7"
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "error")
        expected = format_ingest_error(INGEST_ERROR_PARSER_UNAVAILABLE)
        self.assertEqual(row["error_message"], expected)
        self.assertEqual(row["phase_message"], expected)


class WorkerPassesExceptionObjectTest(unittest.TestCase):
    """The worker must hand the exception OBJECT (not str(e)) to
    _handle_failure, so the persist boundary can redact it (issue #562)."""

    def test_process_task_passes_exception_to_handle_failure(self):
        import asyncio
        from unittest.mock import MagicMock, patch

        from app.services.background_tasks import BackgroundProcessor, TaskItem

        async def run():
            bp = BackgroundProcessor.__new__(BackgroundProcessor)
            captured = {}

            async def failing_run(_self, _task):
                raise ValueError("worker probe failure")

            async def fake_handle(_self, task, error):
                captured["error"] = error

            bp.shutdown_event = asyncio.Event()
            bp.max_retries = 3
            bp.retry_delay = 0.1
            task = TaskItem(file_path="worker-probe.txt", file_id=None, vault_id=7)
            with patch.object(
                BackgroundProcessor, "_run_task_processing", failing_run
            ), patch.object(
                BackgroundProcessor, "_handle_failure", fake_handle
            ), patch.object(
                BackgroundProcessor, "_schedule_retry", MagicMock(return_value=True)
            ):
                await bp._process_task(task)
            return captured["error"]

        error = asyncio.run(run())
        self.assertIsInstance(error, ValueError)


class MarkPermanentlyFailedRedactionTest(unittest.TestCase):
    """The retry-exhausted persist path redacts exceptions, trusts constants."""

    def _processor_with_pool(self, tmp):
        init_db(os.path.join(tmp, "t.db"))
        pool = SQLiteConnectionPool(os.path.join(tmp, "t.db"), max_size=1)
        processor = DocumentProcessor(
            chunk_size_chars=500, chunk_overlap_chars=50, pool=pool
        )
        return processor, pool

    def _make_processor(self, processor):
        from app.services.background_tasks import BackgroundProcessor

        bp = BackgroundProcessor.__new__(BackgroundProcessor)
        bp.processor = processor
        bp.max_retries = 3
        return bp

    def test_exception_payload_is_redacted(self):
        from app.services.background_tasks import TaskItem

        with tempfile.TemporaryDirectory() as tmp:
            processor, pool = self._processor_with_pool(tmp)
            bp = self._make_processor(processor)
            conn = sqlite3.connect(os.path.join(tmp, "t.db"))
            conn.execute(
                "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
                " status) VALUES (1, 7, 'x', 'x.txt', 1, 'processing')"
            )
            conn.commit()
            conn.close()
            task = TaskItem(file_path="whatever.txt", file_id=1, vault_id=7)
            leaky = ValueError("C:\\server\\secret\\uploads\\f.txt raw failure")
            bp._mark_task_permanently_failed(task, leaky)
            pool.close_all()

            conn = sqlite3.connect(os.path.join(tmp, "t.db"))
            row = conn.execute(
                "SELECT error_message FROM files WHERE id = 1"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], format_ingest_error(INGEST_ERROR_PARSE_FAILED))

    def test_string_payload_is_trusted_verbatim(self):
        from app.services.background_tasks import TaskItem

        with tempfile.TemporaryDirectory() as tmp:
            processor, pool = self._processor_with_pool(tmp)
            bp = self._make_processor(processor)
            conn = sqlite3.connect(os.path.join(tmp, "t.db"))
            conn.execute(
                "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
                " status) VALUES (1, 7, 'x', 'x.txt', 1, 'processing')"
            )
            conn.commit()
            conn.close()
            task = TaskItem(file_path="whatever.txt", file_id=1, vault_id=7)
            bp._mark_task_permanently_failed(task, "admission rejected: overloaded")
            pool.close_all()

            conn = sqlite3.connect(os.path.join(tmp, "t.db"))
            row = conn.execute(
                "SELECT error_message FROM files WHERE id = 1"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], "admission rejected: overloaded")


class ClassificationCauseWalkTest(unittest.TestCase):
    """Classification must walk the __cause__ chain the parser wrapper
    builds (issue #562 review: PARSER_UNAVAILABLE was dead otherwise)."""

    def test_wrapped_import_error_maps_to_parser_unavailable(self):
        wrapped = DocumentParseError(
            "Failed to parse document 'C:/x/f.txt': No module named 'unstructured'"
        )
        wrapped.__cause__ = ImportError("No module named 'unstructured'")
        self.assertEqual(
            redact_ingest_error(wrapped),
            "PARSER_UNAVAILABLE: document parser is unavailable",
        )

    def test_wrapped_file_not_found_maps_to_file_missing(self):
        wrapped = DocumentParseError("Failed to read spreadsheet: boom")
        wrapped.__cause__ = FileNotFoundError("Spreadsheet file not found")
        self.assertEqual(
            redact_ingest_error(wrapped),
            "FILE_MISSING: uploaded file is missing from storage",
        )

    def test_unwrapped_failures_keep_top_level_classification(self):
        self.assertEqual(
            redact_ingest_error(ValueError("plain")),
            "PARSE_FAILED: document could not be parsed",
        )


class VaultRelativeFilePathTest(unittest.TestCase):
    """Projection of the stored upload path (issue #562 review fixes)."""

    def test_dot_segments_are_collapsed(self):
        # A path whose dot segments escape the vault collapses to the bare
        # name: no '..' may ever reach the wire.
        self.assertEqual(
            _vault_relative_file_path("C:\\data\\vaults\\1\\uploads\\..\\..\\x"),
            "x",
        )

    def test_interior_dot_segments_collapse_within_the_vault(self):
        self.assertEqual(
            _vault_relative_file_path("/data/vaults/7/uploads/../other.txt"),
            "7/other.txt",
        )

    def test_already_relative_values_fall_back_to_bare_name(self):
        self.assertEqual(
            _vault_relative_file_path("7/uploads/name.pdf"), "name.pdf"
        )

    def test_vault_relative_form_keeps_vault_id_first(self):
        self.assertEqual(
            _vault_relative_file_path("/data/vaults/7/uploads/name.txt"),
            "7/uploads/name.txt",
        )


class DocumentApiResponsePathTest(unittest.TestCase):
    """GET /api/documents must not echo server-absolute file_path (#562)."""

    def setUp(self):
        from fastapi.testclient import TestClient

        from app.api.deps import get_current_active_user, get_db, get_evaluate_policy
        from app.main import app

        self.app = app
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp(prefix="issue562-api-")
        db_path = str(Path(self._temp_dir) / "test.db")
        init_db(db_path)
        self._connection_pool = SimpleConnectionPool(db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        self.app.dependency_overrides[get_db] = override_get_db
        self.app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 42,
            "username": "vault-one-reader",
            "role": "member",
        }

        async def allow_only_vault_one(user, resource_type, resource_id, action):
            return resource_type == "vault" and resource_id == 1 and action == "read"

        self.app.dependency_overrides[get_evaluate_policy] = (
            lambda: allow_only_vault_one
        )

        server_secret = str(Path(self._temp_dir) / "server-secret" / "vaults" / "1" / "uploads")
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description)"
                " VALUES (1, 'Vault 1', '')"
            )
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size,"
                " status, error_message, phase, phase_message)"
                " VALUES (1, ?, 'leak-probe.txt', 100, 'error',"
                " 'PARSE_FAILED: document could not be parsed',"
                " 'error', 'PARSE_FAILED: document could not be parsed')",
                (os.path.join(server_secret, "leak-probe.txt"),),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        from app.api.deps import get_current_active_user, get_db, get_evaluate_policy

        for key in (get_db, get_current_active_user, get_evaluate_policy):
            self.app.dependency_overrides.pop(key, None)
        self._connection_pool.close_all()
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_list_documents_file_path_is_not_server_absolute(self):
        response = self.client.get("/api/documents/?vault_id=1")

        self.assertEqual(response.status_code, 200, response.text)
        docs = response.json()["documents"]
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertEqual(doc["file_name"], "leak-probe.txt")
        self.assertNotIn("server-secret", doc["file_path"])
        self.assertNotIn("\\", doc["file_path"])
        self.assertNotIn(":", doc["file_path"])

    def test_get_document_file_path_is_not_server_absolute(self):
        response = self.client.get("/api/documents/1")

        self.assertEqual(response.status_code, 200, response.text)
        doc = response.json()
        self.assertEqual(doc["file_name"], "leak-probe.txt")
        self.assertNotIn("server-secret", doc["file_path"])
        self.assertNotIn("\\", doc["file_path"])


if __name__ == "__main__":
    unittest.main()
