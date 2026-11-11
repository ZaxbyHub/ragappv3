"""Reviewer-required pin (issue-trace 902-905-911-913 wave 7, review Rev 1):
a finalize FAILURE on the upload route must leave nothing behind.

Post-implementation addition (ships as a plain test file; no repro-check or
checkpoint rows): it pins behavior that landed in the review-fix commit and
asserts it at HEAD.

Contract: driving the REAL upload route with
``DraftInputStorage.finalize`` patched to raise ``OSError`` — the staged
bytes are finalized-nominal but the move fails — the route must answer
500 ``internal_error`` (the existing translation), reserve NO
``draft_inputs`` row (the row inserted before finalize is rolled back),
and discard the staged upload so no ``.part``/final bytes remain anywhere
under the draft input storage root (neither the final project path nor
``.incoming``).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.services.draft_input_storage import DraftInputStorage
from tests.draft_room.test_draft_compile_routes import CompileRouteTestBase


class TestN05GFinalizeFailureDiscard(CompileRouteTestBase):
    def _storage_files(self):
        root = Path(settings.data_dir) / "draft-room"
        if not root.is_dir():
            return []
        return sorted(str(p) for p in root.rglob("*") if p.is_file())

    def _input_row_count(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_inputs WHERE draft_id = ?", (draft_id,)
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def test_finalize_failure_discards_row_and_staged_bytes(self):
        draft_id = self._create_draft(title="N05G finalize discard").json()["id"]

        def _finalize_raises(storage_self, staged, relpath):
            raise OSError("simulated disk failure during the staged->final move")

        with patch.object(DraftInputStorage, "finalize", _finalize_raises):
            resp = self.client.post(
                f"/api/draft-room/drafts/{draft_id}/inputs",
                data={"role": "manuscript", "authority": "primary"},
                files={
                    "file": (
                        "notes.txt",
                        b"bytes whose finalize step fails on disk",
                        "text/plain",
                    )
                },
                headers=self._owner_headers(),
            )

        self.assertEqual(resp.status_code, 500, resp.text)
        self.assertEqual(
            resp.json().get("code"),
            "internal_error",
            f"the finalize failure must surface as 500 internal_error, got {resp.text}",
        )
        self.assertEqual(
            self._input_row_count(draft_id),
            0,
            "a failed finalize must roll back the reserved draft_inputs row",
        )
        self.assertEqual(
            self._storage_files(),
            [],
            "a failed finalize must discard the staged upload — no .part and "
            "no final bytes may remain anywhere under the draft-room storage "
            f"root; found {self._storage_files()}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
