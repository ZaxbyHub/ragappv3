"""Arm's-length route checks for issue #785: single-input Draft Room delete.

Rides the proven ``DraftRoomTestBase`` harness from ``test_draft_routes.py``
(shared app, per-test SQLite pool, draft_room_enabled on). The mutating
endpoint's request-authentication concern is handled entirely by that base
class; this module deliberately stays naive about it.

* AC2 -- deleting an input whose bytes were cited into a completed compile
  (compile job with ``input_id`` NULL — the production writer shape — plus a
  ``draft_evidence`` row with ``source_kind='draft_input'``) must answer
  ``409 input_in_use``, not escape the route as a raw driver error.
* AC6 -- with ``draft_room_enabled`` false, single-input delete must match
  SPEC section 9.2: either it answers ``503 draft_room_disabled`` like the
  other create/edit mutations, or the spec line that defines the disabled
  behaviour must explicitly name input deletion as a cleanup-allowed
  operation. Anything else (e.g. quietly performing the delete) is a
  spec violation.
"""

import asyncio
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
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_store import DraftStore
from draft_room.test_draft_routes import DraftRoomTestBase


class _BytesUpload:
    """Minimal async-readable upload, mirroring the harness in
    ``test_draft_deletion.py`` (only ``read`` is needed by stage_upload)."""

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._content[self._offset :]
            self._offset = len(self._content)
            return chunk
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class TestN01InputDeleteRoutes(DraftRoomTestBase):
    def _seed_input(self, *, used: bool):
        """Seed a draft + stored input directly through DraftStore on the
        harness pool (no route side effects, so no parse job exists).

        ``used=True`` additionally records the production provenance shape:
        a completed compile job whose ``input_id`` is NULL (neither the route
        enqueue nor the worker retry ever sets it) and a ``draft_evidence``
        row with ``source_kind='draft_input'`` citing this input, exactly as
        the compile pipeline writes it (``insert_evidence`` computes
        ``passage_sha256`` from the passage text itself).
        """
        storage = DraftInputStorage(Path(settings.data_dir) / "draft-room")
        conn = self._connection_pool.get_connection()
        try:
            store = DraftStore(conn)
            draft = store.create_draft(
                vault_id=self.READ_VAULT_ID,
                created_by=self.OWNER_ID,
                title="N01 route seed",
                mode="compose",
                tier="standard",
                brief_json="{}",
            )
            staged = asyncio.run(
                storage.stage_upload(
                    _BytesUpload("a.txt", b"manuscript bytes for the route check"),
                    allowed_extensions={".txt"},
                    max_file_bytes=10_000_000,
                )
            )
            record = store.reserve_input(
                draft_id=draft.id,
                owner_id=self.OWNER_ID,
                role="reference",
                authority="unknown",
                as_of_date=None,
                original_name=staged.original_name,
                stored_name=staged.stored_name,
                extension=staged.extension,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                max_inputs=100,
                max_total_input_bytes=10_000_000,
            )
            storage.finalize(staged, record.storage_relpath)
            if used:
                cur = conn.execute(
                    "INSERT INTO draft_jobs (draft_id, vault_id, created_by, "
                    "job_type, status, max_model_calls, timeout_seconds) "
                    "VALUES (?, ?, ?, 'compile', 'completed', 0, 60)",
                    (draft.id, self.READ_VAULT_ID, self.OWNER_ID),
                )
                job_id = int(cur.lastrowid)
                # Commit before insert_evidence: its BEGIN IMMEDIATE rolls
                # back any dangling implicit transaction on the pooled
                # connection, which would otherwise erase this uncommitted
                # job row and fail the evidence FK.
                conn.commit()
                store.insert_evidence(
                    job_id=job_id,
                    label="n01-route-ev-1",
                    source_kind="draft_input",
                    draft_input_id=record.id,
                    title="a.txt",
                    passage="The passage the compile cited from this input.",
                    source_content_sha256=record.content_sha256,
                )
            conn.commit()
            return draft.id, record.id
        finally:
            self._connection_pool.release_connection(conn)

    def _input_count(self, draft_id: int) -> int:
        conn = self._connection_pool.get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM draft_inputs WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            return int(row[0])
        finally:
            self._connection_pool.release_connection(conn)

    def _input_relpath(self, draft_id: int, input_id: int) -> str:
        conn = self._connection_pool.get_connection()
        try:
            row = conn.execute(
                "SELECT storage_relpath FROM draft_inputs WHERE draft_id = ? AND id = ?",
                (draft_id, input_id),
            ).fetchone()
            return str(row[0])
        finally:
            self._connection_pool.release_connection(conn)

    def test_used_input_delete_is_409(self):
        draft_id, input_id = self._seed_input(used=True)
        storage = DraftInputStorage(Path(settings.data_dir) / "draft-room")
        relpath = self._input_relpath(draft_id, input_id)
        before = self._input_count(draft_id)

        resp = self.client.delete(
            f"/api/draft-room/drafts/{draft_id}/inputs/{input_id}",
            headers=self._owner_headers(),
        )

        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "input_in_use")
        # A refusal must not have removed the row or the bytes on the way out.
        self.assertEqual(self._input_count(draft_id), before)
        self.assertTrue(storage.exists(relpath))

    def test_active_compile_delete_is_409_active_job(self):
        draft_id, input_id = self._seed_input(used=False)
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, max_model_calls, timeout_seconds) "
                "VALUES (?, ?, ?, 'compile', 'running', 0, 60)",
                (draft_id, self.READ_VAULT_ID, self.OWNER_ID),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.delete(
            f"/api/draft-room/drafts/{draft_id}/inputs/{input_id}",
            headers=self._owner_headers(),
        )

        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "active_job")

    def test_disabled_input_delete_matches_spec(self):
        # Name retained: the frozen C6 acceptance check selects this node id.
        # Asserted directly rather than against a SPEC-line disjunction: once
        # the spec and the code agree, a doc edit must not be able to satisfy
        # this test for any status.
        draft_id, input_id = self._seed_input(used=False)

        original_enabled = settings.draft_room_enabled
        settings.draft_room_enabled = False
        try:
            resp = self.client.delete(
                f"/api/draft-room/drafts/{draft_id}/inputs/{input_id}",
                headers=self._owner_headers(),
            )
        finally:
            settings.draft_room_enabled = original_enabled

        # Asserted directly rather than against a SPEC-line disjunction: once
        # the spec and the code agree, a doc edit must not be able to satisfy
        # this test for any status.
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()["code"], "draft_room_disabled")


if __name__ == "__main__":
    unittest.main()
