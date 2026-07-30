"""Characterization tests for the upload validators after their move out of
``app.api.routes.documents`` into ``app.services.upload_validation``.

Two things are pinned:
1. The behavior of each validator (magic bytes, OOXML member presence,
   fail-closed zip handling, the >10000-entry guard, filename sanitization).
2. That ``app.api.routes.documents`` still exposes the same five names, bound
   to the same objects, so existing tests that import or patch them through
   the route module keep working.
"""

import io
import os
import sys
import types
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.api.routes import documents
from app.services import upload_validation
from app.services.upload_validation import (
    _MAGIC_BYTES,
    _OOXML_REQUIRED_MEMBERS,
    _check_magic_bytes,
    _validate_ooxml_member,
    secure_filename,
)


class TestRouteModuleStillExposesValidators(unittest.TestCase):
    """The five names must stay bound in the route module's namespace."""

    def test_names_are_the_same_objects(self):
        self.assertIs(documents._MAGIC_BYTES, upload_validation._MAGIC_BYTES)
        self.assertIs(
            documents._OOXML_REQUIRED_MEMBERS,
            upload_validation._OOXML_REQUIRED_MEMBERS,
        )
        self.assertIs(documents._check_magic_bytes, upload_validation._check_magic_bytes)
        self.assertIs(
            documents._validate_ooxml_member, upload_validation._validate_ooxml_member
        )
        self.assertIs(documents.secure_filename, upload_validation.secure_filename)

    def test_upload_route_resolves_validators_through_module_globals(self):
        """Patching the route module's global must reach the upload handler.

        The handler looks the names up at call time via the module globals, so
        ``patch("app.api.routes.documents.secure_filename")`` remains effective.
        """
        handler = documents._do_upload
        names = handler.__code__.co_names
        self.assertIn("secure_filename", names)
        self.assertIn("_check_magic_bytes", names)
        self.assertIn("_validate_ooxml_member", names)
        self.assertIn("_OOXML_REQUIRED_MEMBERS", names)
        # None of them are closure/local captures.
        self.assertEqual(handler.__code__.co_freevars, ())
        for name in (
            "secure_filename",
            "_check_magic_bytes",
            "_validate_ooxml_member",
            "_OOXML_REQUIRED_MEMBERS",
        ):
            self.assertNotIn(name, handler.__code__.co_varnames)


class TestMagicBytes(unittest.TestCase):
    def test_signature_table_is_unchanged(self):
        self.assertEqual(
            _MAGIC_BYTES,
            {
                ".pdf": b"%PDF",
                ".docx": b"PK\x03\x04",
                ".xlsx": b"PK\x03\x04",
                ".pptx": b"PK\x03\x04",
                ".xls": b"\xd0\xcf\x11\xe0",
            },
        )

    def test_accepts_matching_headers(self):
        self.assertTrue(_check_magic_bytes(".pdf", b"%PDF-1.7\n"))
        self.assertTrue(_check_magic_bytes(".docx", b"PK\x03\x04\x14\x00\x00\x00"))
        self.assertTrue(_check_magic_bytes(".xlsx", b"PK\x03\x04\x14\x00\x00\x00"))
        self.assertTrue(_check_magic_bytes(".pptx", b"PK\x03\x04\x14\x00\x00\x00"))
        self.assertTrue(_check_magic_bytes(".xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"))

    def test_rejects_mismatched_headers(self):
        self.assertFalse(_check_magic_bytes(".pdf", b"PK\x03\x04"))
        self.assertFalse(_check_magic_bytes(".docx", b"%PDF-1.7"))
        self.assertFalse(_check_magic_bytes(".xls", b"PK\x03\x04"))
        # Truncated header cannot match.
        self.assertFalse(_check_magic_bytes(".pdf", b"%PD"))
        self.assertFalse(_check_magic_bytes(".pdf", b""))

    def test_unlisted_extension_is_allowed(self):
        for ext in (".txt", ".md", ".csv", ".json", ".yaml", ".unknown", ""):
            self.assertTrue(_check_magic_bytes(ext, b"anything at all"))

    def test_extension_lookup_is_case_sensitive(self):
        # The route lowercases the suffix before calling; ".PDF" is not in the
        # table and therefore passes through unchecked.
        self.assertTrue(_check_magic_bytes(".PDF", b"not a pdf"))


class TestOOXMLMemberValidation(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _zip(self, name: str, members) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w") as zf:
            for member in members:
                zf.writestr(member, "x")
        return path

    def test_required_member_table_is_unchanged(self):
        self.assertEqual(
            _OOXML_REQUIRED_MEMBERS,
            {
                ".docx": "word/document.xml",
                ".xlsx": "xl/workbook.xml",
                ".pptx": "ppt/presentation.xml",
            },
        )

    def test_accepts_archive_containing_required_member(self):
        for ext, member in _OOXML_REQUIRED_MEMBERS.items():
            path = self._zip(f"ok{ext}", [member, "[Content_Types].xml"])
            self.assertTrue(_validate_ooxml_member(path, member), ext)

    def test_rejects_archive_missing_required_member(self):
        path = self._zip("plain.docx", ["not-ooxml.txt"])
        self.assertFalse(_validate_ooxml_member(path, "word/document.xml"))

    def test_rejects_cross_typed_ooxml(self):
        path = self._zip("cross.xlsx", ["word/document.xml"])
        self.assertFalse(_validate_ooxml_member(path, "xl/workbook.xml"))

    def test_member_match_is_exact_not_prefix_or_suffix(self):
        path = self._zip("near.docx", ["evil/word/document.xml", "word/document.xml.bak"])
        self.assertFalse(_validate_ooxml_member(path, "word/document.xml"))

    def test_bad_zip_fails_closed(self):
        path = self.tmp / "corrupt.docx"
        path.write_bytes(b"PK\x03\x04not-actually-a-zip")
        self.assertFalse(_validate_ooxml_member(path, "word/document.xml"))

    def test_missing_path_fails_closed(self):
        self.assertFalse(
            _validate_ooxml_member(self.tmp / "absent.docx", "word/document.xml")
        )

    def test_directory_path_fails_closed(self):
        self.assertFalse(_validate_ooxml_member(self.tmp, "word/document.xml"))

    def test_accepts_string_and_path_inputs(self):
        path = self._zip("both.docx", ["word/document.xml"])
        self.assertTrue(_validate_ooxml_member(path, "word/document.xml"))
        self.assertTrue(_validate_ooxml_member(str(path), "word/document.xml"))

    def test_entry_count_guard_rejects_oversized_central_directory(self):
        # 10000 entries is allowed; 10001 trips the guard even though the
        # required member is present.
        at_limit = self.tmp / "at_limit.docx"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "x")
            for i in range(9999):
                zf.writestr(f"f{i}.txt", "")
        at_limit.write_bytes(buf.getvalue())
        self.assertTrue(_validate_ooxml_member(at_limit, "word/document.xml"))

        over_limit = self.tmp / "over_limit.docx"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "x")
            for i in range(10000):
                zf.writestr(f"f{i}.txt", "")
        over_limit.write_bytes(buf.getvalue())
        self.assertFalse(_validate_ooxml_member(over_limit, "word/document.xml"))


class TestSecureFilename(unittest.TestCase):
    def test_strips_traversal(self):
        self.assertEqual(secure_filename("../../etc/passwd"), "passwd")
        self.assertEqual(secure_filename("/etc/passwd"), "passwd")
        # POSIX basename does not split on backslash; dots survive the
        # allow-list, so only the separators are removed.
        self.assertEqual(
            secure_filename("..\\..\\windows\\system32"), "....windowssystem32"
        )
        self.assertEqual(secure_filename("subdir/report.pdf"), "report.pdf")

    def test_spaces_become_underscores(self):
        self.assertEqual(secure_filename("my report final.docx"), "my_report_final.docx")

    def test_non_ascii_is_removed(self):
        self.assertEqual(secure_filename("résumé.pdf"), "rsum.pdf")
        self.assertEqual(secure_filename("文档.txt"), ".txt")
        self.assertEqual(secure_filename("naïve file.md"), "nave_file.md")

    def test_disallowed_punctuation_is_removed(self):
        self.assertEqual(secure_filename("a;b|c&d$e.txt"), "abcde.txt")
        self.assertEqual(secure_filename("re:port(1).txt"), "report1.txt")
        self.assertEqual(secure_filename("keep-these_1.2.txt"), "keep-these_1.2.txt")

    def test_empty_and_whitespace_input(self):
        self.assertEqual(secure_filename(""), "")
        self.assertEqual(secure_filename("   "), "___")
        self.assertEqual(secure_filename("///"), "")
        self.assertEqual(secure_filename("文档"), "")

    def test_dot_segments(self):
        self.assertEqual(secure_filename(".."), "..")
        self.assertEqual(secure_filename("."), ".")

    def test_result_has_no_separators(self):
        for candidate in ("../../etc/passwd", "a/b/c.txt", "..\\..\\x.txt"):
            result = secure_filename(candidate)
            self.assertNotIn("/", result)
            self.assertNotIn("\\", result)


if __name__ == "__main__":
    unittest.main()
