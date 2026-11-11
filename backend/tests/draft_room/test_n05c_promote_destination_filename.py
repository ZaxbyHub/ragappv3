"""Arm's-length acceptance check C3 for the draft-capability-liveness trace
(issues #902/#905/#911/#913), promotion destination side.

Frozen specification. DISCRIMINATING: RED at base, GREEN after the fix.

* C3 -- promoting a draft titled ``Q3/Q4 plan`` must land a destination
  document whose filename stem starts with ``Q3_Q4_plan`` (separator
  rewritten as content, mirroring the export rule pinned by issue #789),
  with no path separator in the filename and no traversal outside the
  destination vault's upload directory. At base
  ``DraftPromotion._destination_filename`` feeds the raw title to
  ``secure_filename``, which strips everything before the ``/`` and yields
  ``Q4_plan.txt``.

Drives the REAL promote route via ``DraftPromoteTestBase``.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.config import settings
from tests.draft_room.test_draft_promote import DraftPromoteTestBase

TITLE = "Q3/Q4 plan"


class TestN05CPromoteDestinationFilename(DraftPromoteTestBase):
    """C3 (DISCRIMINATING)."""

    def test_promote_title_separator_lands_in_destination_filename(self):
        """RED at base (destination ``Q4_plan.txt``), GREEN after the fix."""
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=b"promote with a slashed title")
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, title=TITLE
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        body = resp.json()

        filename = body["filename"]
        stem = filename.rsplit(".", 1)[0]
        self.assertTrue(
            stem.startswith("Q3_Q4_plan"),
            f"the promoted document's filename stem must start with Q3_Q4_plan, "
            f"got {filename!r}",
        )
        self.assertNotIn(
            "/",
            filename,
            f"the promoted document's filename must contain no forward slash, "
            f"got {filename!r}",
        )
        self.assertNotIn(
            "\\",
            filename,
            f"the promoted document's filename must contain no backslash, "
            f"got {filename!r}",
        )

        files = self._files_rows()
        self.assertEqual(len(files), 1)
        dest_path = Path(files[0]["file_path"]).resolve()
        upload_dir = settings.vault_uploads_dir(self.READ_VAULT_ID).resolve()
        try:
            relative = dest_path.relative_to(upload_dir)
        except ValueError:
            self.fail(
                f"the promoted document must land inside the vault upload dir "
                f"{upload_dir}, got {dest_path}"
            )
        self.assertTrue(
            relative.parts and relative.parts[0] != "..",
            "the promoted document must not traverse outside the upload dir",
        )

    def test_promote_backslash_title_separator_lands_in_destination_filename(self):
        """RED at base (destination ``Q4_plan.txt`` — the basename-first
        sanitizer treats a backslash as a path separator too), GREEN after
        the fix: a ``\\`` inside the TITLE is content, not a path."""
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(
            draft_id, content=b"promote with a backslashed title"
        )
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, title="Q3\\Q4 plan"
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        filename = resp.json()["filename"]
        stem = filename.rsplit(".", 1)[0]
        self.assertTrue(
            stem.startswith("Q3_Q4_plan"),
            f"the promoted document's filename stem must start with Q3_Q4_plan, "
            f"got {filename!r}",
        )
        self.assertNotIn("/", filename, f"no forward slash allowed: {filename!r}")
        self.assertNotIn("\\", filename, f"no backslash allowed: {filename!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
