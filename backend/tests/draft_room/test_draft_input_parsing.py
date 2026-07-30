"""Tests for the parse-only extraction seam (app.services.document_extraction).

``unstructured`` is absent from the reduced CI dependency set, so
``DocumentParser.parse`` is exercised against a stubbed
``unstructured.partition.auto.partition``. The stub reads the real temp file
from disk and returns its content as parser elements, so the file-on-disk →
normalized-text path is genuinely exercised; element ordering across multiple
elements is pinned by its own test.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.services.document_extraction import (
    DocumentExtractionError,
    DocumentExtractionService,
    ExtractedDocument,
)
from app.services.document_processor import DocumentParseError, DocumentParser

_UNSTRUCTURED_MODULES = (
    "unstructured",
    "unstructured.partition",
    "unstructured.partition.auto",
)


class _StubElement:
    """Minimal stand-in for an unstructured element (only ``str()`` is used)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


def _read_file_as_single_element(filename, **_kwargs):
    # read_bytes, not read_text: read_text applies universal-newline
    # translation, which would hide the service's own CRLF normalization.
    return [_StubElement(Path(filename).read_bytes().decode("utf-8"))]


class _PartitionStubMixin:
    """Installs a fake ``unstructured`` package for the duration of a test."""

    def install_partition(self, impl):
        for name in _UNSTRUCTURED_MODULES:
            self._saved_modules[name] = sys.modules.get(name)
        pkg = types.ModuleType("unstructured")
        pkg.__path__ = []
        partition_pkg = types.ModuleType("unstructured.partition")
        partition_pkg.__path__ = []
        auto = types.ModuleType("unstructured.partition.auto")
        auto.partition = impl
        partition_pkg.auto = auto
        pkg.partition = partition_pkg
        sys.modules["unstructured"] = pkg
        sys.modules["unstructured.partition"] = partition_pkg
        sys.modules["unstructured.partition.auto"] = auto

    def setUp(self):
        self._saved_modules = {}
        self._temp_dir = tempfile.mkdtemp()
        self.service = DocumentExtractionService()

    def tearDown(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def write(self, name: str, content: str) -> Path:
        path = Path(self._temp_dir) / name
        path.write_text(content, encoding="utf-8", newline="")
        return path


class TestExtractTextRoundTrip(_PartitionStubMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.install_partition(_read_file_as_single_element)

    def test_txt_round_trip_preserves_paragraph_breaks(self):
        content = "First paragraph line one.\nstill paragraph one.\n\nSecond paragraph.\n"
        path = self.write("note.txt", content)

        result = self.service.extract_text(path)

        self.assertIsInstance(result, ExtractedDocument)
        self.assertEqual(result.text, content)
        self.assertIn("\n\n", result.text)
        self.assertEqual(result.character_count, len(result.text))
        self.assertEqual(result.media_type, "text/plain")
        self.assertEqual(result.warnings, [])

    def test_crlf_is_normalized_and_trailing_spaces_stripped(self):
        path = self.write(
            "windows.txt",
            "Alpha line   \r\n\r\nBeta line\t\r\nGamma\r",
        )

        result = self.service.extract_text(path)

        self.assertEqual(result.text, "Alpha line\n\nBeta line\nGamma\n")
        self.assertNotIn("\r", result.text)
        self.assertEqual(result.character_count, len(result.text))

    def test_markdown_headings_lists_quotes_and_numbers_survive(self):
        content = (
            "# Chapter 1\n"
            "\n"
            'He said "straight quotes" and she said “curly quotes”.\n'
            "It's an apostrophe; so is ’this’.\n"
            "\n"
            "- item one\n"
            "- item two\n"
            "  - nested item\n"
            "\n"
            "1. first\n"
            "2. second\n"
            "\n"
            "Totals: 1,234.56 and 0042 and -7.\n"
        )
        path = self.write("chapter.md", content)

        result = self.service.extract_text(path)

        # Byte-exact: no smart-quote folding, no whitespace collapsing, no
        # unicode normalization, list/heading boundaries intact.
        self.assertEqual(result.text, content)
        self.assertEqual(result.text.encode("utf-8"), content.encode("utf-8"))
        self.assertIn('"straight quotes"', result.text)
        self.assertIn("“curly quotes”", result.text)
        self.assertIn("’this’", result.text)
        self.assertIn("  - nested item", result.text)
        self.assertIn("1,234.56", result.text)
        self.assertEqual(result.character_count, len(content))

    def test_media_type_uses_stdlib_mimetypes_with_octet_stream_fallback(self):
        import mimetypes

        md_result = self.service.extract_text(self.write("a.md", "text"))
        expected_md = mimetypes.guess_type("a.md")[0] or "application/octet-stream"
        # `.md` is only in the stdlib map on systems that ship /etc/mime.types,
        # so pin it to the stdlib answer rather than a hardcoded string.
        self.assertEqual(md_result.media_type, expected_md)

        unknown = self.service.extract_text(self.write("a.zzzunknown", "text"))
        self.assertEqual(unknown.media_type, "application/octet-stream")

    def test_empty_document_warning_and_no_content_in_warnings(self):
        result = self.service.extract_text(self.write("blank.txt", "   \n  \n"))

        self.assertEqual(result.warnings, ["empty_document"])
        self.assertEqual(result.text.strip(), "")
        self.assertEqual(result.character_count, len(result.text))


class TestElementOrdering(_PartitionStubMixin, unittest.TestCase):
    def test_elements_are_joined_with_newline_in_parser_order(self):
        def multi_element(filename, **_kwargs):
            return [
                _StubElement("Chapter One"),
                _StubElement("The opening paragraph."),
                _StubElement("- bullet a"),
                _StubElement("- bullet b"),
            ]

        self.install_partition(multi_element)
        path = self.write("ordered.txt", "ignored by stub")

        result = self.service.extract_text(path)

        self.assertEqual(
            result.text,
            "Chapter One\nThe opening paragraph.\n- bullet a\n- bullet b",
        )


class TestExtractionErrors(_PartitionStubMixin, unittest.TestCase):
    def test_missing_file_maps_to_input_file_missing(self):
        missing = Path(self._temp_dir) / "does-not-exist.txt"

        with self.assertRaises(DocumentExtractionError) as ctx:
            self.service.extract_text(missing)

        self.assertEqual(ctx.exception.code, "input_file_missing")
        self.assertIn("FileNotFoundError", str(ctx.exception))
        self.assertNotIn(str(missing), str(ctx.exception))
        self.assertNotIn("does-not-exist", str(ctx.exception))

    def test_parser_failure_message_is_redacted(self):
        path = self.write("manuscript.txt", "content")
        leaky = "secret manuscript text /abs/path"

        with patch.object(
            DocumentParser, "parse", side_effect=DocumentParseError(leaky)
        ):
            with self.assertRaises(DocumentExtractionError) as ctx:
                self.service.extract_text(path)

        message = str(ctx.exception)
        self.assertEqual(ctx.exception.code, "input_parse_failed")
        self.assertIn("DocumentParseError", message)
        self.assertNotIn("secret manuscript text", message)
        self.assertNotIn("/abs/path", message)
        self.assertNotIn(str(path), message)
        self.assertLessEqual(len(message), 200)

    def test_unexpected_exception_also_maps_to_input_parse_failed(self):
        path = self.write("boom.txt", "content")

        with patch.object(
            DocumentParser, "parse", side_effect=RuntimeError("secret manuscript text")
        ):
            with self.assertRaises(DocumentExtractionError) as ctx:
                self.service.extract_text(path)

        self.assertEqual(ctx.exception.code, "input_parse_failed")
        self.assertIn("RuntimeError", str(ctx.exception))
        self.assertNotIn("secret manuscript text", str(ctx.exception))

    def test_long_underlying_message_is_truncated(self):
        path = self.write("long.txt", "content")

        with patch.object(
            DocumentParser, "parse", side_effect=DocumentParseError("x" * 5000)
        ):
            with self.assertRaises(DocumentExtractionError) as ctx:
                self.service.extract_text(path)

        self.assertLessEqual(len(str(ctx.exception)), 200)
        self.assertNotIn("xxxx", str(ctx.exception))


class TestNoSideEffects(_PartitionStubMixin, unittest.TestCase):
    """extract_text must touch nothing but the filesystem path it is given."""

    def setUp(self):
        super().setUp()
        self.install_partition(_read_file_as_single_element)

    def test_module_source_has_no_indexing_or_embedding_references(self):
        from app.services import document_extraction

        source = Path(document_extraction.__file__).read_text(encoding="utf-8")

        for forbidden in (
            "VectorStore",
            "vector_store",
            "EmbeddingService",
            "chunk",
            "lancedb",
            "BackgroundProcessor",
            "enqueue",
            "INSERT",
            "UPDATE",
        ):
            self.assertNotIn(forbidden, source, f"unexpected reference: {forbidden}")

    def test_extract_text_does_not_write_the_files_table(self):
        from app.models.database import (
            _pool_cache,
            _pool_cache_lock,
            init_db,
            run_migrations,
        )

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        db_path = str(Path(self._temp_dir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            # init_db seeds vault 1; seed one pre-existing row so the
            # before/after comparison is not trivially 0 == 0.
            vault_id = conn.execute("SELECT MIN(id) FROM vaults").fetchone()[0]
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size)"
                " VALUES (?, '/pre/existing.txt', 'existing.txt', 1)",
                (vault_id,),
            )
            conn.commit()
            before = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

            result = self.service.extract_text(self.write("draft.txt", "Hello draft."))

            after = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            names = [r[0] for r in conn.execute("SELECT file_name FROM files")]
        finally:
            conn.close()

        self.assertEqual(result.text, "Hello draft.")
        self.assertEqual(before, 1)
        self.assertEqual(after, before)
        self.assertEqual(names, ["existing.txt"])

    def test_no_lancedb_directory_or_stray_files_are_created(self):
        target = self.write("solo.txt", "Only file.")
        before = sorted(p.name for p in Path(self._temp_dir).iterdir())

        self.service.extract_text(target)

        after = sorted(p.name for p in Path(self._temp_dir).iterdir())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
