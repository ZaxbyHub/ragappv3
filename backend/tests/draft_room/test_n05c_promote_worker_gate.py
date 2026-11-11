"""Arm's-length acceptance check C18 for the draft-capability-liveness trace
(issues #902/#905/#911/#913), promote route gating side.

Frozen specification. DISCRIMINATING: RED at base, GREEN after the fix.

* C18 -- when no background processor is available (both
  ``app.state.background_processor`` None AND the
  ``get_background_processor`` dependency resolving to None — the seam
  ``test_draft_promote.py``'s base wires with a mock, left None here),
  POST /api/draft-room/drafts/{id}/promote must answer HTTP 503 with error
  code ``worker_unavailable`` and create NOTHING: no ``files`` row, no
  ``draft_promotions`` row, and no bytes copied into the vault upload dir.
  A second leg injects (and mounts) a REAL ``BackgroundProcessor`` that was
  never started — same 503 gate, nothing created.
  Base behavior, for both legs: the None processor makes the promotion
  service raise on the missing ``enqueue`` and the route answers 500
  ``promotion_failed_incomplete_cleanup``; the mounted never-started
  processor accepts the enqueue onto its (workerless) queue and the route
  answers 202. Neither is a worker gate, so the 503 itself is the fix.
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

from fastapi.testclient import TestClient

from app.api.deps import get_background_processor
from app.config import settings
from app.main import app
from app.services.background_tasks import BackgroundProcessor
from tests.draft_room.test_draft_promote import DraftPromoteTestBase
from tests.draft_room.test_n05c_capability_liveness import set_app_state


class TestN05CPromoteWorkerGate(DraftPromoteTestBase):
    """C18 (DISCRIMINATING)."""

    def _vault_upload_files(self):
        upload_dir = settings.vault_uploads_dir(self.READ_VAULT_ID).resolve()
        if not upload_dir.is_dir():
            return []
        return sorted(str(p) for p in upload_dir.rglob("*") if p.is_file())

    def _gated_promote(self, draft_id, input_id, title):
        """POST promote through a no-raise client (the established
        TestN05UploadCompensation idiom): whatever the base does with a
        dead worker must surface as a response, never as an exception
        blowing up the test call."""
        client = TestClient(app, raise_server_exceptions=False)
        client.headers["user-agent"] = ""
        return client.post(
            f"/api/draft-room/drafts/{draft_id}/promote",
            json={"source_type": "input", "source_id": input_id, "title": title},
            headers=self._owner_headers(),
        )

    def _ready_input(self, content):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=content)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return draft_id, input_id

    def _assert_gated_and_barren(self, resp, draft_id, before_bytes):
        self.assertEqual(
            resp.status_code,
            503,
            f"with no live worker the promote route must answer 503, got "
            f"{resp.status_code}: {resp.text}",
        )
        self.assertEqual(
            resp.json().get("code"),
            "worker_unavailable",
            f"the 503 must carry error code worker_unavailable, got {resp.text}",
        )
        self.assertEqual(
            len(self._files_rows()),
            0,
            "a gated promote must create no files row",
        )
        self.assertEqual(
            len(self._promotion_rows(draft_id)),
            0,
            "a gated promote must create no draft_promotions row",
        )
        self.assertEqual(
            self._vault_upload_files(),
            before_bytes,
            "a gated promote must copy no bytes into the vault upload dir",
        )

    def test_promote_without_a_background_processor_answers_503_and_creates_nothing(
        self,
    ):
        draft_id, input_id = self._ready_input(b"promote with no live worker")
        before_bytes = self._vault_upload_files()

        restore_state = set_app_state("background_processor", None)
        app.dependency_overrides[get_background_processor] = lambda: None
        try:
            resp = self._gated_promote(draft_id, input_id, "Gated promotion")
        finally:
            app.dependency_overrides[get_background_processor] = lambda: (
                self._mock_background_processor
            )
            restore_state()

        self._assert_gated_and_barren(resp, draft_id, before_bytes)

    def test_promote_with_a_real_processor_that_never_started_answers_503(self):
        """Same contract with a REAL ``BackgroundProcessor`` injected (and on
        app.state) that was never started: no ingestion worker loops are
        behind it, so promotion is not available. RED at base (the promotion
        service runs and fails on the not-running processor — or silently
        accepts — either way not a 503 worker gate), GREEN after the fix."""
        draft_id, input_id = self._ready_input(b"promote with a never-started worker")
        before_bytes = self._vault_upload_files()

        processor = BackgroundProcessor()
        restore_state = set_app_state("background_processor", processor)
        app.dependency_overrides[get_background_processor] = lambda: processor
        try:
            resp = self._gated_promote(draft_id, input_id, "Gated promotion")
        finally:
            app.dependency_overrides[get_background_processor] = lambda: (
                self._mock_background_processor
            )
            restore_state()

        self._assert_gated_and_barren(resp, draft_id, before_bytes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
