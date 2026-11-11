"""Arm's-length acceptance check C2 for the draft-capability-liveness trace
(issues #902/#905/#911/#913), canvas download side.

Frozen specification. DISCRIMINATING: RED at base, GREEN after the fix.

* C2 -- downloading a canvas artifact named ``A/B notes`` must yield a
  download filename whose stem starts with ``A_B_notes`` and contains no
  path separator. At base, ``_download_filename`` feeds the raw name to
  ``secure_filename``, which strips everything before a ``/`` (the
  #789 defect class), so ``A/B notes`` downloads as ``B_notes.py``.

Drives the REAL canvas routes (create through
``POST /api/chat/sessions/{id}/artifacts``, download through
``GET /api/canvas/artifacts/{uid}/versions/1/download``) on the shared
``CanvasTestBase`` fixture.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from tests.test_canvas_artifacts import CanvasTestBase


def _filename_from_disposition(disposition: str) -> str:
    marker = 'filename="'
    start = disposition.index(marker) + len(marker)
    return disposition[start : disposition.index('"', start)]


class TestCanvasExportSeparatorFilename(CanvasTestBase):
    """C2 (DISCRIMINATING)."""

    def test_download_filename_keeps_separator_as_content(self):
        """RED at base (``B_notes.py``), GREEN after the fix: a ``/`` inside
        an artifact NAME is content, not a path — the download filename must
        rewrite it to an underscore instead of truncating the title."""
        session_id = self._make_session()
        created = self._create_artifact(
            session_id,
            name="A/B notes",
            content="print('separator is content')\n",
            language="python",
        )
        self.assertEqual(created.status_code, 200, created.text)
        uid = created.json()["artifact_uid"]

        response = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/1/download",
            headers=self._super_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("attachment", response.headers["Content-Disposition"])

        filename = _filename_from_disposition(response.headers["Content-Disposition"])
        stem = filename.rsplit(".", 1)[0]
        self.assertTrue(
            stem.startswith("A_B_notes"),
            f"the download filename stem must start with A_B_notes, got {filename!r}",
        )
        self.assertNotIn(
            "/",
            filename,
            f"the download filename must contain no forward slash, got {filename!r}",
        )
        self.assertNotIn(
            "\\",
            filename,
            f"the download filename must contain no backslash, got {filename!r}",
        )

    def test_download_filename_keeps_backslash_separator_as_content(self):
        """RED at base (``B_notes.py`` — the basename-first sanitizer treats a
        backslash as a path separator too), GREEN after the fix: a ``\\``
        inside an artifact NAME is content, not a path — the download
        filename must rewrite it to an underscore, keeping the text before
        it."""
        session_id = self._make_session()
        created = self._create_artifact(
            session_id,
            name="A\\B notes",
            content="print('backslash is content too')\n",
            language="python",
        )
        self.assertEqual(created.status_code, 200, created.text)
        uid = created.json()["artifact_uid"]

        response = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions/1/download",
            headers=self._super_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("attachment", response.headers["Content-Disposition"])

        filename = _filename_from_disposition(response.headers["Content-Disposition"])
        stem = filename.rsplit(".", 1)[0]
        self.assertTrue(
            stem.startswith("A_B_notes"),
            f"the download filename stem must start with A_B_notes, got {filename!r}",
        )
        self.assertNotIn("/", filename, f"no forward slash allowed: {filename!r}")
        self.assertNotIn("\\", filename, f"no backslash allowed: {filename!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
