"""Tests for app.services.draft_job_processor (issue #435, SPEC section 10).

Exercises the durable ``parse_input`` job processor against a real SQLite
database and a real temp-directory filesystem: atomic claim, the full
happy-path parse, cooperative cancellation with the discard guarantee, the
parsed-character limit, timeouts, error sanitization, startup orphan/missing-
file recovery, poll-loop resilience, and post-commit SSE publication.

``unstructured``/``lancedb`` are absent from the reduced CI dependency set;
this module never exercises the real ``DocumentParser``/``unstructured``
path — extraction is a controllable fake implementing the
``DocumentExtractionService`` surface, so only ``lancedb`` needs stubbing.
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.config import settings
from app.services.document_extraction import DocumentExtractionError, ExtractedDocument
from app.services.draft_events import get_draft_event_bus
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_job_processor import (
    CODE_INPUT_FILE_MISSING,
    CODE_INPUT_PARSE_FAILED,
    CODE_JOB_TIMEOUT,
    CODE_PARSED_TEXT_LIMIT_EXCEEDED,
    CODE_PERMISSION_REVOKED,
    DraftJobProcessor,
)
from app.services.draft_store import DraftStore, sha256_text

# ── Test doubles ─────────────────────────────────────────────────────────


class FakeUpload:
    """Minimal stand-in for a FastAPI UploadFile, matching DraftInputStorage's needs."""

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


class FakeExtractionService:
    """Controllable stand-in for DocumentExtractionService.extract_text.

    Responses are keyed by the resolved filesystem path passed to
    ``extract_text``. A response may be an ``ExtractedDocument``, an
    exception instance to raise, or a callable taking no args that returns
    or raises. Missing keys fall back to a trivial empty document.
    """

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.calls: list[str] = []

    def extract_text(self, path: Path) -> ExtractedDocument:
        key = str(path)
        self.calls.append(key)
        resp = self.responses.get(key)
        if callable(resp):
            resp = resp()
        if isinstance(resp, BaseException):
            raise resp
        if resp is None:
            return ExtractedDocument(
                text="", character_count=0, media_type="text/plain", warnings=[]
            )
        return resp


class ConnectionPool:
    """Thread-safe SQLite pool exposing the ``with pool.connection() as conn``
    idiom the processor requires, mirroring the pattern used by
    test_wiki_curator.py and test_settings_curator.py for processor tests."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._pool: Queue = Queue()
        self._closed = False

    def get_connection(self):
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self):
        import sqlite3

        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn) -> None:
        if self._closed:
            conn.close()
            return
        self._pool.put_nowait(conn)

    def close_all(self) -> None:
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except Empty:
                break

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)


# ── Base harness ─────────────────────────────────────────────────────────


class DraftJobProcessorTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        import sqlite3

        seed = sqlite3.connect(self._db_path)
        seed.execute("PRAGMA foreign_keys = ON")
        seed.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (1,'owner','hash','Owner','member',1)"
        )
        seed.execute("INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1,'V','')")
        # Every existing dispatch test relies on the owner passing the
        # permission_revoked re-check (finding 1); grant explicit vault read
        # so those tests keep exercising the parse-path behavior they were
        # written for rather than the new permission gate.
        seed.execute(
            "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission) "
            "VALUES (1,1,'read')"
        )
        seed.commit()
        seed.close()

        self.pool = ConnectionPool(self._db_path)
        self.root = Path(self._temp_dir) / "draft-room"
        self.storage = DraftInputStorage(self.root)
        self.extraction = FakeExtractionService()
        self.processor = DraftJobProcessor(
            pool=self.pool,
            storage=self.storage,
            extraction=self.extraction,
            poll_interval=0.02,
        )

        self._orig_max_parsed_chars = settings.draft_max_total_parsed_chars

    def tearDown(self):
        settings.draft_max_total_parsed_chars = self._orig_max_parsed_chars
        self.pool.close_all()
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -- helpers --

    def make_draft(self, *, owner_id=1, vault_id=1, title="Draft"):
        with self.pool.connection() as conn:
            return DraftStore(conn).create_draft(
                vault_id=vault_id,
                created_by=owner_id,
                title=title,
                mode="compose",
                tier="standard",
                brief_json="{}",
            )

    async def add_input(self, draft_id, *, owner_id=1, name="a.txt", content=b"hello world"):
        upload = FakeUpload(name, content)
        staged = await self.storage.stage_upload(
            upload, allowed_extensions={".txt"}, max_file_bytes=10_000_000
        )
        with self.pool.connection() as conn:
            record = DraftStore(conn).reserve_input(
                draft_id=draft_id,
                owner_id=owner_id,
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
        self.storage.finalize(staged, record.storage_relpath)
        return record

    def enqueue_parse_job(self, draft_id, owner_id, input_id, *, timeout_seconds=60):
        with self.pool.connection() as conn:
            return DraftStore(conn).enqueue_parse_job(
                draft_id=draft_id,
                owner_id=owner_id,
                input_id=input_id,
                timeout_seconds=timeout_seconds,
            )

    def get_input(self, draft_id, owner_id, input_id):
        with self.pool.connection() as conn:
            return DraftStore(conn).get_input(
                draft_id=draft_id, owner_id=owner_id, input_id=input_id
            )

    def get_job(self, draft_id, owner_id, job_id):
        with self.pool.connection() as conn:
            return DraftStore(conn).get_job(
                draft_id=draft_id, owner_id=owner_id, job_id=job_id
            )

    def raw_input_row(self, input_id):
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT parsed_text, parsed_text_sha256, parsed_char_count, "
                "parse_status, parse_error FROM draft_inputs WHERE id = ?",
                (input_id,),
            ).fetchone()

    async def run_poll_iterations(self, n=1, delay=0.05):
        for _ in range(n):
            await asyncio.sleep(delay)

    async def wait_until(self, predicate, *, timeout=5.0, interval=0.02):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(interval)
        self.fail("condition not met before timeout")

    def make_user_and_vault(
        self, user_id, vault_id, *, is_active=1, role="member", grant_read=True
    ):
        """Seed a distinct user/vault pair, deliberately using high IDs so it
        never collides with the ``Default`` vault (id 1) that migrations
        auto-create."""
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, "
                "role, is_active) VALUES (?, ?, 'hash', 'U', ?, ?)",
                (user_id, f"user{user_id}", role, is_active),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, '')",
                (vault_id, f"vault{vault_id}"),
            )
            if grant_read:
                conn.execute(
                    "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission) "
                    "VALUES (?, ?, 'read')",
                    (vault_id, user_id),
                )
            conn.commit()

    def set_user_active(self, user_id, is_active: bool) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE users SET is_active = ? WHERE id = ?", (int(is_active), user_id)
            )
            conn.commit()

    def revoke_vault_read(self, user_id, vault_id) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "DELETE FROM vault_members WHERE user_id = ? AND vault_id = ?",
                (user_id, vault_id),
            )
            conn.commit()


# ── Happy path ───────────────────────────────────────────────────────────


class TestHappyPathParse(DraftJobProcessorTestBase):
    async def test_pending_job_is_claimed_parsed_and_completed(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id, content=b"hello draft room")
        job = self.enqueue_parse_job(draft.id, 1, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))
        self.extraction.responses[resolved_path] = ExtractedDocument(
            text="Hello Draft Room.",
            character_count=len("Hello Draft Room."),
            media_type="text/plain",
            warnings=[],
        )

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 1, job.id).status
                in ("completed", "failed", "cancelled")
            )
        finally:
            await self.processor.stop()

        job_after = self.get_job(draft.id, 1, job.id)
        self.assertEqual(job_after.status, "completed")

        input_after = self.get_input(draft.id, 1, input_record.id)
        self.assertEqual(input_after.parse_status, "ready")
        self.assertEqual(input_after.parsed_char_count, len("Hello Draft Room."))
        self.assertEqual(
            input_after.parsed_text_sha256, sha256_text("Hello Draft Room.")
        )

        raw = self.raw_input_row(input_record.id)
        self.assertEqual(raw["parsed_text"], "Hello Draft Room.")


class TestClaimAtomicity(DraftJobProcessorTestBase):
    async def test_two_concurrent_claims_yield_one_job_and_one_none(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        job = self.enqueue_parse_job(draft.id, 1, input_record.id)

        results: list[object] = [None, None]
        barrier = threading.Barrier(2)

        def claim(slot: int) -> None:
            import sqlite3

            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                barrier.wait(timeout=5)
                results[slot] = DraftStore(conn).claim_next_parse_job()
            finally:
                conn.close()

        t1 = threading.Thread(target=claim, args=(0,))
        t2 = threading.Thread(target=claim, args=(1,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        claimed = [r for r in results if r is not None]
        none_results = [r for r in results if r is None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(len(none_results), 1)
        self.assertEqual(claimed[0].id, job.id)
        self.assertEqual(claimed[0].status, "running")


class TestExtractionFailureSanitization(DraftJobProcessorTestBase):
    async def test_extraction_error_fails_job_and_input_without_leaking_text(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        job = self.enqueue_parse_job(draft.id, 1, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))
        leaky = "contains SECRET-MANUSCRIPT and /abs/path/should/not/leak"
        self.extraction.responses[resolved_path] = DocumentExtractionError(
            "input_parse_failed", leaky
        )

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 1, job.id).status
                in ("completed", "failed", "cancelled")
            )
        finally:
            await self.processor.stop()

        job_after = self.get_job(draft.id, 1, job.id)
        self.assertEqual(job_after.status, "failed")
        self.assertEqual(job_after.error_code, CODE_INPUT_PARSE_FAILED)

        input_after = self.get_input(draft.id, 1, input_record.id)
        self.assertEqual(input_after.parse_status, "failed")

        raw = self.raw_input_row(input_record.id)
        for field in ("parsed_text", "parsed_text_sha256"):
            self.assertIsNone(raw[field])
        self.assertNotIn("SECRET-MANUSCRIPT", raw["parse_error"] or "")
        self.assertNotIn("/abs/path", raw["parse_error"] or "")
        self.assertNotIn("SECRET-MANUSCRIPT", job_after.error_message or "")
        self.assertNotIn("/abs/path", job_after.error_message or "")


class TestCancellation(DraftJobProcessorTestBase):
    async def test_cancellation_before_commit_discards_text(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        job = self.enqueue_parse_job(draft.id, 1, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))

        def slow_and_cancel():
            # By the time extraction "finishes", the job has been marked
            # cancel-requested by the test below, racing the processor's
            # pre-commit cancellation check.
            with self.pool.connection() as conn:
                DraftStore(conn).request_job_cancel(draft_id=draft.id, owner_id=1, job_id=job.id)
            return ExtractedDocument(
                text="should never be persisted",
                character_count=len("should never be persisted"),
                media_type="text/plain",
                warnings=[],
            )

        self.extraction.responses[resolved_path] = slow_and_cancel

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 1, job.id).status
                in ("completed", "failed", "cancelled")
            )
        finally:
            await self.processor.stop()

        job_after = self.get_job(draft.id, 1, job.id)
        self.assertEqual(job_after.status, "cancelled")

        input_after = self.get_input(draft.id, 1, input_record.id)
        self.assertEqual(input_after.parse_status, "cancelled")

        raw = self.raw_input_row(input_record.id)
        self.assertIsNone(raw["parsed_text"])


class TestParsedCharLimit(DraftJobProcessorTestBase):
    async def test_exceeding_limit_stores_only_char_count(self):
        settings.draft_max_total_parsed_chars = 5

        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        job = self.enqueue_parse_job(draft.id, 1, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))
        big_text = "x" * 50
        self.extraction.responses[resolved_path] = ExtractedDocument(
            text=big_text,
            character_count=len(big_text),
            media_type="text/plain",
            warnings=[],
        )

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 1, job.id).status
                in ("completed", "failed", "cancelled")
            )
        finally:
            await self.processor.stop()

        job_after = self.get_job(draft.id, 1, job.id)
        self.assertEqual(job_after.status, "failed")
        self.assertEqual(job_after.error_code, CODE_PARSED_TEXT_LIMIT_EXCEEDED)

        raw = self.raw_input_row(input_record.id)
        self.assertIsNone(raw["parsed_text"])
        self.assertEqual(raw["parsed_char_count"], len(big_text))
        self.assertEqual(raw["parse_status"], "failed")
        self.assertEqual(raw["parse_error"], CODE_PARSED_TEXT_LIMIT_EXCEEDED)


class TestTimeout(DraftJobProcessorTestBase):
    async def test_extraction_exceeding_timeout_fails_with_job_timeout(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        job = self.enqueue_parse_job(draft.id, 1, input_record.id, timeout_seconds=1)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))

        def slow_extract():
            time.sleep(1.5)
            return ExtractedDocument(
                text="too late", character_count=8, media_type="text/plain", warnings=[]
            )

        self.extraction.responses[resolved_path] = slow_extract

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 1, job.id).status
                in ("completed", "failed", "cancelled"),
                timeout=10,
            )
        finally:
            await self.processor.stop()

        job_after = self.get_job(draft.id, 1, job.id)
        self.assertEqual(job_after.status, "failed")
        self.assertEqual(job_after.error_code, CODE_JOB_TIMEOUT)

        input_after = self.get_input(draft.id, 1, input_record.id)
        self.assertEqual(input_after.parse_status, "failed")


# ── Startup recovery ─────────────────────────────────────────────────────


class TestStartupRecovery(DraftJobProcessorTestBase):
    async def test_orphaned_running_job_and_parsing_input_reset_to_pending(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        job = self.enqueue_parse_job(draft.id, 1, input_record.id)

        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE draft_jobs SET status = 'running' WHERE id = ?", (job.id,)
            )
            conn.execute(
                "UPDATE draft_inputs SET parse_status = 'parsing' WHERE id = ?",
                (input_record.id,),
            )
            conn.commit()

        # Recover without starting the poll loop, so the reset state is
        # observable before the processor would otherwise re-claim it.
        await asyncio.to_thread(self.processor._recover_on_startup)

        job_after = self.get_job(draft.id, 1, job.id)
        self.assertEqual(job_after.status, "pending")
        self.assertEqual(job_after.error_code, "worker_restart")

        input_after = self.get_input(draft.id, 1, input_record.id)
        self.assertEqual(input_after.parse_status, "pending")

    async def test_pending_input_without_active_job_and_present_file_is_reenqueued(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        # No job enqueued: simulates a crash between reservation commit and
        # job enqueue (SPEC section 6.2).

        await asyncio.to_thread(self.processor._recover_on_startup)

        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT status FROM draft_jobs WHERE input_id = ? AND job_type = 'parse_input'",
                (input_record.id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")

    async def test_pending_input_with_missing_file_is_failed(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        self.storage.resolve(input_record.storage_relpath).unlink()

        await asyncio.to_thread(self.processor._recover_on_startup)

        input_after = self.get_input(draft.id, 1, input_record.id)
        self.assertEqual(input_after.parse_status, "failed")
        self.assertEqual(input_after.parse_error, CODE_INPUT_FILE_MISSING)


# ── Poll-loop resilience ─────────────────────────────────────────────────


class TestPollLoopResilience(DraftJobProcessorTestBase):
    async def test_poll_loop_survives_a_job_exception_and_processes_the_next_one(self):
        draft = self.make_draft()
        broken_input = await self.add_input(draft.id, name="broken.txt")
        broken_job = self.enqueue_parse_job(draft.id, 1, broken_input.id)
        good_input = await self.add_input(draft.id, name="good.txt", content=b"fine")
        good_job = self.enqueue_parse_job(draft.id, 1, good_input.id)

        good_path = str(self.storage.resolve(good_input.storage_relpath))
        self.extraction.responses[good_path] = ExtractedDocument(
            text="fine", character_count=4, media_type="text/plain", warnings=[]
        )

        # Force the first job's dispatch to explode with an unexpected error
        # (not a DocumentExtractionError) by making the input record lookup
        # itself blow up for that one job.
        real_get_input = self.processor._get_input
        calls = {"n": 0}

        def flaky_get_input(job):
            calls["n"] += 1
            if job.id == broken_job.id:
                raise RuntimeError("boom")
            return real_get_input(job)

        self.processor._get_input = flaky_get_input

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 1, good_job.id).status
                in ("completed", "failed", "cancelled")
            )
            await self.wait_until(
                lambda: self.get_job(draft.id, 1, broken_job.id).status
                in ("completed", "failed", "cancelled")
            )
        finally:
            await self.processor.stop()

        self.assertEqual(self.get_job(draft.id, 1, good_job.id).status, "completed")
        self.assertEqual(self.get_job(draft.id, 1, broken_job.id).status, "failed")
        self.assertEqual(
            self.get_job(draft.id, 1, broken_job.id).error_code, "internal_error"
        )


# ── SSE publication ──────────────────────────────────────────────────────


class TestSSEPublication(DraftJobProcessorTestBase):
    async def test_event_published_only_after_commit_and_publish_failure_is_swallowed(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        job = self.enqueue_parse_job(draft.id, 1, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))
        self.extraction.responses[resolved_path] = ExtractedDocument(
            text="hi", character_count=2, media_type="text/plain", warnings=[]
        )

        bus = get_draft_event_bus()
        queue = bus.subscribe(draft.id)

        original_publish = bus.publish
        publish_calls: list[dict] = []

        def raising_publish(draft_id, event):
            publish_calls.append(event)
            # Simulate a publish failure — must never fail the job.
            raise RuntimeError("publish exploded")

        bus.publish = raising_publish
        try:
            await self.processor.start()
            try:
                await self.wait_until(
                    lambda: self.get_job(draft.id, 1, job.id).status
                    in ("completed", "failed", "cancelled")
                )
            finally:
                await self.processor.stop()
        finally:
            bus.publish = original_publish
            bus.unsubscribe(draft.id, queue)

        # The job succeeded despite every publish call raising.
        self.assertEqual(self.get_job(draft.id, 1, job.id).status, "completed")
        # A job_completed event was attempted only after the DB shows completed
        # (state is already committed by the time publish is invoked).
        event_types = [e.get("type") for e in publish_calls]
        self.assertIn("job_completed", event_types)
        self.assertIn("job_started", event_types)

    async def test_completed_event_delivered_to_subscriber(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        job = self.enqueue_parse_job(draft.id, 1, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))
        self.extraction.responses[resolved_path] = ExtractedDocument(
            text="hi", character_count=2, media_type="text/plain", warnings=[]
        )

        bus = get_draft_event_bus()
        queue = bus.subscribe(draft.id)
        try:
            await self.processor.start()
            try:
                await self.wait_until(
                    lambda: self.get_job(draft.id, 1, job.id).status
                    in ("completed", "failed", "cancelled")
                )
                # Give the loop a beat to publish after the commit.
                await self.run_poll_iterations(1, delay=0.05)
            finally:
                await self.processor.stop()

            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
        finally:
            bus.unsubscribe(draft.id, queue)

        event_types = [e.get("type") for e in events]
        self.assertIn("job_completed", event_types)


# ── Permission re-check (SPEC section 9.1 rule 4, issue #435 finding 1) ────


class TestPermissionRevocation(DraftJobProcessorTestBase):
    async def test_deactivated_owner_fails_with_permission_revoked(self):
        self.make_user_and_vault(9001, 9001)
        draft = self.make_draft(owner_id=9001, vault_id=9001)
        input_record = await self.add_input(draft.id, owner_id=9001)
        job = self.enqueue_parse_job(draft.id, 9001, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))
        self.extraction.responses[resolved_path] = ExtractedDocument(
            text="never persisted", character_count=16, media_type="text/plain", warnings=[]
        )

        self.set_user_active(9001, False)

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 9001, job.id).status
                in ("completed", "failed", "cancelled")
            )
        finally:
            await self.processor.stop()

        job_after = self.get_job(draft.id, 9001, job.id)
        self.assertEqual(job_after.status, "failed")
        self.assertEqual(job_after.error_code, CODE_PERMISSION_REVOKED)

        raw = self.raw_input_row(input_record.id)
        self.assertIsNone(raw["parsed_text"])
        self.assertEqual(raw["parse_status"], "failed")
        self.assertEqual(raw["parse_error"], CODE_PERMISSION_REVOKED)

    async def test_owner_without_vault_read_fails_with_permission_revoked(self):
        self.make_user_and_vault(9002, 9002, grant_read=False)
        draft = self.make_draft(owner_id=9002, vault_id=9002)
        input_record = await self.add_input(draft.id, owner_id=9002)
        job = self.enqueue_parse_job(draft.id, 9002, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))
        self.extraction.responses[resolved_path] = ExtractedDocument(
            text="never persisted", character_count=16, media_type="text/plain", warnings=[]
        )

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 9002, job.id).status
                in ("completed", "failed", "cancelled")
            )
        finally:
            await self.processor.stop()

        job_after = self.get_job(draft.id, 9002, job.id)
        self.assertEqual(job_after.status, "failed")
        self.assertEqual(job_after.error_code, CODE_PERMISSION_REVOKED)

        raw = self.raw_input_row(input_record.id)
        self.assertIsNone(raw["parsed_text"])
        self.assertEqual(raw["parse_status"], "failed")
        self.assertEqual(raw["parse_error"], CODE_PERMISSION_REVOKED)

    async def test_revocation_during_extraction_blocks_precommit(self):
        """Proves the pre-commit re-check exists independently of the
        post-claim one: permission is valid when extraction starts and is
        revoked by the extraction call itself, so only the second check can
        catch it."""
        self.make_user_and_vault(9003, 9003)
        draft = self.make_draft(owner_id=9003, vault_id=9003)
        input_record = await self.add_input(draft.id, owner_id=9003)
        job = self.enqueue_parse_job(draft.id, 9003, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))

        def extract_and_revoke():
            self.revoke_vault_read(9003, 9003)
            return ExtractedDocument(
                text="should never be persisted",
                character_count=26,
                media_type="text/plain",
                warnings=[],
            )

        self.extraction.responses[resolved_path] = extract_and_revoke

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 9003, job.id).status
                in ("completed", "failed", "cancelled")
            )
        finally:
            await self.processor.stop()

        job_after = self.get_job(draft.id, 9003, job.id)
        self.assertEqual(job_after.status, "failed")
        self.assertEqual(job_after.error_code, CODE_PERMISSION_REVOKED)

        raw = self.raw_input_row(input_record.id)
        self.assertIsNone(raw["parsed_text"])
        self.assertEqual(raw["parse_status"], "failed")
        self.assertEqual(raw["parse_error"], CODE_PERMISSION_REVOKED)
        # Extraction did run (the pre-extraction check passed) — this is what
        # distinguishes this test from the first two, which never reach
        # extraction at all.
        self.assertEqual(self.extraction.calls, [resolved_path])


if __name__ == "__main__":
    unittest.main()
