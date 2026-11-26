"""Shared-parser forwarding regression (issue #1162, final-critic round 1).

The binary content-type refusal must live in the WIKI compile fallback
(wiki_compiler._wiki_refuses_content_type), never in the shared
DocumentParser.parse: main ingestion parses NUL-leading binary formats
(PDF/DOCX/PPTX are ZIP/binary containers) through that parser. These
tests pin both directions under the conftest unstructured stub.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "real_docs"


class TestParserForwardsBinaryFormats(unittest.TestCase):
    """DocumentParser.parse forwards supported binary formats to partition()."""

    def _parse_returns_elements(self, name: str) -> list:
        from app.services.document_processor import DocumentParser

        seen: list = []
        real_partition = None
        try:
            from unstructured.partition.auto import partition as real_partition
        except Exception:
            real_partition = None

        def spy_partition(*args, **kwargs):
            seen.append(args[0] if args else kwargs.get("filename"))
            if real_partition is not None and not getattr(
                spy_partition, "_stub", False
            ):
                return real_partition(*args, **kwargs)
            return []

        # Force the stub path deterministically: the conftest stub may or may
        # not be installed depending on the environment, so patch the lazy
        # import target module attribute directly.
        import sys
        import types

        mod = sys.modules.get("unstructured.partition.auto") or types.ModuleType(
            "unstructured.partition.auto"
        )
        with patch.object(mod, "partition", spy_partition), patch.dict(
            sys.modules, {"unstructured.partition.auto": mod}
        ):
            elements = DocumentParser().parse(str(FIXTURES / name))
        self.assertEqual(seen, [str(FIXTURES / name)], "partition was not forwarded")
        return list(elements)

    def test_docx_reaches_partition(self):
        self._parse_returns_elements("sample_table.docx")

    def test_pdf_reaches_partition(self):
        self._parse_returns_elements("sample_table.pdf")

    def test_pptx_reaches_partition(self):
        self._parse_returns_elements("sample_table.pptx")


class TestWikiRefusalAllowlist(unittest.TestCase):
    """The typed refusal is decided in the wiki fallback by allowlist+sniff."""

    def test_supported_binary_document_suffix_is_not_refused(self):
        from app.services.wiki_compiler import _wiki_refuses_content_type

        self.assertFalse(_wiki_refuses_content_type(str(FIXTURES / "sample_table.docx")))
        self.assertFalse(_wiki_refuses_content_type(str(FIXTURES / "sample_table.pdf")))
        self.assertFalse(_wiki_refuses_content_type(str(FIXTURES / "sample_table.pptx")))

    def test_unsupported_binary_suffix_is_refused(self):
        import tempfile

        from app.services.wiki_compiler import _wiki_refuses_content_type

        tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        try:
            tmp.write(b"\x00\x01\x02\x03\xff\xfe\xfd\xfc")
            tmp.close()
            self.assertTrue(_wiki_refuses_content_type(tmp.name))
        finally:
            import os

            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def test_text_suffix_with_binary_head_is_not_pre_refused(self):
        """Allowlisted extensions always reach the parser (the .txt U+FFFD
        guard downstream handles masquerading text) — the pre-refusal never
        fires for a supported extension."""
        import tempfile

        from app.services.wiki_compiler import _wiki_refuses_content_type

        tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        try:
            tmp.write(b"\x00\x01\x02")
            tmp.close()
            self.assertFalse(_wiki_refuses_content_type(tmp.name))
        finally:
            import os

            try:
                os.unlink(tmp.name)
            except OSError:
                pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
