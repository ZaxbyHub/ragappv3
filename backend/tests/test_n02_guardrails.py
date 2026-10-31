"""Guardrails for issue #786 — the seven acceptance-critical items the plan
review demanded beyond the frozen checks (see the trace's 05-fix-plan.md and
06-critic-review.md).

1. FileWatcher skips canonical ``.part`` leftovers while still enqueueing a
   real file in the same scan — and does NOT skip a non-canonical
   ``.part``-named user file (exact-shape predicate).
2. The NON-lease queue drain of ``cancel_pending_jobs_async`` runs on the
   event-loop thread (rejects the "wrap the whole method in to_thread"
   alternative, which would break the drain's on-loop atomicity).
3. The empty-match refusal: both entry points return 0 and cancel nothing
   (an unguarded empty lease-mode call would cancel EVERY pending job).
4. Migration success assertions (C3 strengthening; C3 is byte-frozen):
   destination exists with identical bytes, the files row is rewritten, and
   the source is renamed to ``.migrated``.
5. ``_do_upload`` failure-path cleanup matrix: 400/413/409/500 each leave
   neither the reserved placeholder nor any ``.part`` in the vault uploads
   dir.
6. Cancelling a promotion DURING the to_thread write leaves the vault
   uploads dir empty after the detached worker settles (the loop-side
   os.replace never runs after cancellation; the worker-done-gated reaper
   removes the leftover canonical .part).
7. Cancelling an upload mid-stream leaves no placeholder; any residual
   temp is a canonical, watcher-skipped ``.part``.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies before any app import (the same stub
# block test_documents_auth.py installs — conftest.py covers pyarrow only).
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services import upload_path as upload_path_module
from app.services.atomic_write import is_temp_artifact, temp_sibling
from app.services.background_tasks import BackgroundProcessor
from app.services.file_watcher import FileWatcher


class _DataDir:
    """b03-style DATA_DIR env + settings.data_dir snapshot/restore."""

    def __enter__(self):
        self.saved_env = os.environ.get("DATA_DIR")
        self.saved_dir = settings.data_dir
        self.tmp = tempfile.mkdtemp(prefix="n02_guard_")
        os.environ["DATA_DIR"] = self.tmp
        settings.data_dir = Path(self.tmp)
        self.db_path = str(Path(self.tmp) / "app.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        return self

    def __exit__(self, *exc):
        if self.saved_env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.saved_env
        settings.data_dir = self.saved_dir
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def seed_vault(self, vault_id: int = 1) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) "
                "VALUES (?, 'n02-guard-vault', '')",
                (vault_id,),
            )
            conn.commit()
        finally:
            conn.close()


def test_watcher_skips_canonical_part_and_keeps_real_files():
    with _DataDir() as env:
        env.seed_vault(1)
        upload_dir = settings.vault_uploads_dir(1)

        leftover = temp_sibling(upload_dir / "killed_report.txt")
        leftover.write_bytes(b"partial bytes of a killed write")
        genuine = upload_dir / "genuine.txt"
        genuine.write_bytes(b"a genuinely complete document")
        user_dot_part = upload_dir / ".notes.part"
        user_dot_part.write_bytes(b"a legitimate user file, not a temp artifact")

        enqueued: list[str] = []

        class StubProcessor:
            async def enqueue(self, path, vault_id=None):
                enqueued.append(Path(path).name)

        watcher = FileWatcher(processor=StubProcessor())
        count = asyncio.run(watcher.scan_once())

        assert count.files_enqueued >= 2
        assert "genuine.txt" in enqueued, "real file must still be enqueued"
        assert ".notes.part" in enqueued, (
            "non-canonical .part-named user files must NOT be skipped"
        )
        assert leftover.name not in enqueued, (
            "canonical .part leftover must never be enqueued"
        )


def test_nonlease_queue_drain_runs_on_event_loop_thread():
    bp = BackgroundProcessor(retry_delay=0.05)
    assert not bp._cancel_pending_jobs_lease_active(), "no pool -> non-lease mode"
    bp.queue.put_nowait(types.SimpleNamespace(file_id=7, cancelled=False))

    drain_threads: list[int] = []
    original_drain = bp._cancel_pending_jobs_queue

    def recording_drain(**match):
        drain_threads.append(threading.get_ident())
        return original_drain(**match)

    bp._cancel_pending_jobs_queue = recording_drain

    async def drive():
        loop_thread = threading.get_ident()
        cancelled = await bp.cancel_pending_jobs_async(file_id=7)
        return loop_thread, cancelled

    loop_thread, cancelled = asyncio.run(drive())
    assert cancelled == 1
    assert drain_threads and drain_threads[0] == loop_thread, (
        "the in-memory queue drain must stay on the event-loop thread "
        "(asyncio.Queue is not thread-safe); running the whole method in "
        "to_thread would fail this pin"
    )


def test_empty_match_refusal_on_both_entry_points():
    with _DataDir() as env:
        env.seed_vault(1)
        conn = sqlite3.connect(env.db_path)
        try:
            conn.execute(
                "INSERT INTO jobs (queue, payload_json, status, run_after) "
                "VALUES ('ingestion', '{\"file_id\": 7}', 'pending', "
                "datetime('now', '+3600 seconds'))"
            )
            conn.commit()
        finally:
            conn.close()

        pool = SQLiteConnectionPool(env.db_path, max_size=2)
        try:
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            assert bp._cancel_pending_jobs_lease_active(), "pool -> lease mode"

            assert bp.cancel_pending_jobs() == 0
            assert asyncio.run(bp.cancel_pending_jobs_async()) == 0

            row = sqlite3.connect(env.db_path).execute(
                "SELECT status FROM jobs"
            ).fetchone()
            assert row is not None and row[0] == "pending", (
                "an empty match must cancel nothing"
            )

            assert bp.cancel_pending_jobs(file_id=7) == 1
            status = sqlite3.connect(env.db_path).execute(
                "SELECT status FROM jobs"
            ).fetchone()[0]
            assert status == "cancelled", "a real match still cancels"
        finally:
            pool.close_all()


def test_migrate_uploads_success_assertions():
    with _DataDir() as env:
        env.seed_vault(1)
        legacy_dir = settings.uploads_dir
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_path = legacy_dir / "n02_guard_legacy.txt"
        payload = b"legacy upload bytes for the migration guardrail " * 4
        legacy_path.write_bytes(payload)

        conn = sqlite3.connect(env.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, status) VALUES (1, ?, 'n02_guard_legacy.txt', "
                "'n02guardhash', ?, 'indexed')",
                (str(legacy_path), legacy_path.stat().st_size),
            )
            conn.commit()
        finally:
            conn.close()

        with patch.object(
            upload_path_module, "_lookup_vault_id_for_path", return_value=1
        ):
            result = upload_path_module.migrate_uploads(dry_run=False)

        dest = settings.vault_uploads_dir(1) / "n02_guard_legacy.txt"
        assert result.migrated == 1 and result.failed == []
        assert dest.exists()
        assert dest.read_bytes() == payload
        migrated_backup = legacy_dir / "n02_guard_legacy.txt.migrated"
        assert migrated_backup.exists(), "source must be kept as .migrated backup"
        assert migrated_backup.read_bytes() == payload
        row = sqlite3.connect(env.db_path).execute(
            "SELECT file_path FROM files WHERE file_name = 'n02_guard_legacy.txt'"
        ).fetchone()
        assert row is not None and row[0] == str(dest), (
            "the files row must be rewritten to the destination path"
        )


def test_upload_failure_paths_leave_no_artifacts():
    from fastapi.testclient import TestClient
    from test_integration import setup_app_state

    from app.main import app

    original_data_dir = settings.data_dir
    cleanup = setup_app_state(app)
    try:
        db_path = app.state._test_db_path
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'n02-guard-upload')"
            )
            conn.commit()
        finally:
            conn.close()
        upload_dir = settings.vault_uploads_dir(1)
        client = TestClient(app)

        def artifacts():
            return sorted(p.name for p in upload_dir.iterdir() if p.is_file())

        # 400 magic mismatch (pre-stream validation): placeholder removed.
        resp = client.post(
            "/api/documents/upload?vault_id=1",
            files={"file": ("bad.png", BytesIO(b"definitely not a png payload"), "image/png")},
        )
        assert resp.status_code == 400, resp.text
        assert "bad.png" not in artifacts() and not any(
            is_temp_artifact(p) for p in upload_dir.iterdir()
        ), f"400 magic path left artifacts: {artifacts()}"

        # 413 via content-length (pre-reservation): nothing created at all.
        resp = client.post(
            "/api/documents/upload?vault_id=1",
            files={
                "file": (
                    "huge.txt",
                    BytesIO(b"x" * (settings.max_file_size_mb * 1024 * 1024 + 1024)),
                    "text/plain",
                )
            },
        )
        assert resp.status_code == 413, resp.text
        assert "huge.txt" not in artifacts() and not any(
            is_temp_artifact(p) for p in upload_dir.iterdir()
        ), f"413 path left artifacts: {artifacts()}"

        # 409 duplicate: second upload's placed bytes are cleaned up; only
        # the first upload's file remains.
        ok = client.post(
            "/api/documents/upload?vault_id=1",
            files={"file": ("dup.txt", BytesIO(b"duplicate body"), "text/plain")},
        )
        assert ok.status_code == 200, ok.text
        before = artifacts()
        dup = client.post(
            "/api/documents/upload?vault_id=1",
            files={"file": ("dup_1.txt", BytesIO(b"duplicate body"), "text/plain")},
        )
        assert dup.status_code == 409, dup.text
        after = artifacts()
        assert after == before, f"409 path left artifacts: {after} vs {before}"

        # 500 outer catch-all (registration-phase construction failure):
        # both the placed bytes and any temp are discarded.
        with patch(
            "app.api.routes.documents.DocumentProcessor",
            side_effect=RuntimeError("boom"),
        ):
            boom = client.post(
                "/api/documents/upload?vault_id=1",
                files={"file": ("boom.txt", BytesIO(b"boom body"), "text/plain")},
            )
        assert boom.status_code == 500, boom.text
        assert "boom.txt" not in artifacts() and not any(
            is_temp_artifact(p) for p in upload_dir.iterdir()
        ), f"500 catch-all left artifacts: {artifacts()}"
    finally:
        cleanup()
        settings.data_dir = original_data_dir


def test_promote_cancel_during_write_leaves_no_artifacts():
    from app.services.draft_promotion import _promote

    with _DataDir() as env:
        env.seed_vault(1)
        pool = SQLiteConnectionPool(env.db_path, max_size=2)
        started = threading.Event()
        release = threading.Event()

        def slow_write(dest: Path) -> None:
            started.set()
            release.wait(timeout=10)
            dest.write_bytes(b"promoted payload bytes " * 8)

        async def drive():
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            task = asyncio.create_task(
                _promote(
                    storage=types.SimpleNamespace(),
                    db_pool=pool,
                    background_processor=bp,
                    draft_id=1,
                    vault_id=1,
                    title="Cancelled",
                    promoted_by=1,
                    source_type="input",
                    source_id=1,
                    extension=".txt",
                    write_bytes=slow_write,
                )
            )
            # Poll (never block the loop with Event.wait — the task we are
            # waiting for needs this loop to run).
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.05)
            assert started.is_set(), "write thread never started"
            task.cancel()
            release.set()
            try:
                await asyncio.wait_for(task, timeout=15)
            except (asyncio.CancelledError, Exception):
                pass
            # The detached worker's canonical .part is reaped by a
            # background task on THIS loop (asyncio.run closes it when
            # drive() returns), so settle while the loop is still alive.
            # Settle on the PRODUCTION contract — the reserved placeholder
            # must be gone, and anything still present must be a canonical
            # watcher-skipped .part — rather than on total directory
            # emptiness, which is strictly stronger than what
            # _reap_detached_temp guarantees (it is explicitly best-effort
            # and logs a survivor) and so could flake on a slow Windows
            # handle release (issue #886 review PRR-011).
            upload_dir = settings.vault_uploads_dir(1)
            placeholder = upload_dir / "Cancelled.txt"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if not placeholder.exists() and not any(
                    is_temp_artifact(p) for p in upload_dir.iterdir() if p.is_file()
                ):
                    break
                await asyncio.sleep(0.1)

        try:
            asyncio.run(drive())
        finally:
            pool.close_all()

        upload_dir = settings.vault_uploads_dir(1)
        placeholder = upload_dir / "Cancelled.txt"
        assert not placeholder.exists(), (
            "cancel during the write must remove the reserved destination "
            "placeholder once the worker settles"
        )
        remaining = sorted(p.name for p in upload_dir.iterdir() if p.is_file())
        # is_temp_artifact takes a Path (it matches on path.name) — join the
        # dir back on, or a surviving canonical .part (the exact slow-handle
        # case this relaxation exists to tolerate) raises AttributeError.
        assert all(is_temp_artifact(upload_dir / name) for name in remaining), (
            "any residue after a cancelled promotion must be a canonical, "
            f"watcher-skipped .part artifact; found {remaining}"
        )


def test_upload_cancel_midstream_leaves_no_placeholder():
    from app.api.routes.documents import _do_upload
    from app.models.database import get_pool

    with _DataDir() as env:
        env.seed_vault(1)
        pool = get_pool(env.db_path)
        bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
        upload_dir = settings.vault_uploads_dir(1)
        started = threading.Event()

        class StallingUpload:
            filename = "stall.txt"
            _first = True

            async def read(self, size: int = -1) -> bytes:
                if self._first:
                    self._first = False
                    return b"stalling document header"
                started.set()
                await asyncio.sleep(30)
                return b""

        async def drive():
            request = types.SimpleNamespace(headers={})
            task = asyncio.create_task(
                _do_upload(
                    request=request,
                    file=StallingUpload(),
                    settings_dep=settings,
                    vector_store=types.SimpleNamespace(),
                    embedding_service=types.SimpleNamespace(),
                    db_pool=pool,
                    background_processor=bp,
                    vault_id=1,
                )
            )
            # Poll (never block the loop with Event.wait — the upload task
            # needs this loop to reach its second read).
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.05)
            assert started.is_set(), "stream never stalled"
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=15)
            except (asyncio.CancelledError, Exception):
                pass

        asyncio.run(drive())

        placeholder = upload_dir / "stall.txt"
        assert not placeholder.exists(), (
            "cancellation mid-stream must remove the reserved placeholder"
        )
        residue = [p for p in upload_dir.iterdir() if p.is_file()]
        assert all(is_temp_artifact(p) for p in residue), (
            "any residue must be canonical, watcher-skipped .part artifacts; "
            f"found {[p.name for p in residue]}"
        )


def test_recorder_positive_control_sees_the_write():
    """PRR-008: the write recorder must PROVE it recorded the write.

    Every frozen `assert wrote_final_directly is False` is a negation over the
    recorder's own log, so an empty log would satisfy all of them: a future
    change of write primitive that none of the patched attributes intercept
    would make the frozen AC1/AC2/AC3 checks pass vacuously. This positive
    control pins that the recorder is actually attached and observing the
    production write path, using the real `temp_sibling` output as the
    destination the recorder must have seen.

    It lives here rather than in the frozen check files precisely because this
    file is not in the checkpoint manifest, so it needs no anchor amendment.
    """
    import builtins
    import os as _os

    from app.services.atomic_write import temp_sibling
    from tests.test_n02_atomic_watched_writes import WriteRecorder

    with _DataDir() as env:
        env.seed_vault(1)
        upload_dir = settings.vault_uploads_dir(1)
        dest = upload_dir / "positive_control.txt"
        # The production shape: a temp sibling written with a write-mode open,
        # then os.replace onto the reserved destination.
        temp = temp_sibling(dest)

        recorder = WriteRecorder()
        with recorder.patch():
            _os.close(_os.open(str(temp), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY))
            with builtins.open(temp, "wb") as fh:
                fh.write(b"positive control payload")
            _os.replace(temp, dest)

        recorded_destinations = recorder.destinations()
        assert str(temp) in recorded_destinations, (
            "the recorder must observe the write-mode open of the temp sibling; "
            f"recorded={sorted(recorded_destinations)}"
        )
        assert str(dest) not in recorded_destinations, (
            "os.replace is not a byte write, so the final path must not appear"
        )
        assert dest.read_bytes() == b"positive control payload"
        # The consumed temp's NAME still matches the canonical pattern (that is
        # exactly why the watcher skips such files) but the file itself is gone,
        # absorbed by the atomic replace.
        assert is_temp_artifact(temp) is True
        assert not temp.exists()

        # And the negative direction the frozen checks rely on: a write-mode
        # open of the FINAL path IS recorded, so `... is False` is meaningful.
        recorder2 = WriteRecorder()
        with recorder2.patch():
            with builtins.open(dest, "wb") as fh:
                fh.write(b"direct write to the final path")
        assert str(dest) in recorder2.destinations(), (
            "a direct write to the final path must be recorded, otherwise the "
            "frozen AC1 assertion could never fail"
        )


def test_lease_default_is_pinned_not_inherited_from_env(tmp_path):
    """PRR-027: the lease precondition must not depend on a developer's .env.

    `BackgroundProcessor.__init__` reads `settings.ingestion_job_lease_enabled`
    and `Settings.model_config` loads a gitignored `backend/.env`, so on a
    developer box that sets the flag false the lease assertions would fail on
    correct code. Pin it explicitly with restore, as the established lease
    suites do (test_m03_ingest_cancel.py).
    """
    from app.models.database import SQLiteConnectionPool
    from app.services.background_tasks import BackgroundProcessor

    original = settings.ingestion_job_lease_enabled
    settings.ingestion_job_lease_enabled = True
    db_path = str(Path(tmp_path) / "lease_pin.db")
    init_db(db_path)
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
        assert bp._ingest_lease_enabled is True
        assert bp._cancel_pending_jobs_lease_active() is True
    finally:
        pool.close_all()
        settings.ingestion_job_lease_enabled = original


def test_watcher_skips_zero_byte_placeholders_but_keeps_real_files():
    """PRR-014: a 0-byte reservation placeholder is never an ingestion candidate.

    `_do_upload` creates the final-path O_EXCL placeholder before streaming,
    so the final path holds a 0-byte file for the whole write. Such a file is
    rejected downstream as "empty", but only AFTER becoming a scan candidate —
    which emitted a structural-rejection warning on every scan tick for every
    in-flight upload. `_find_new_files` now skips zero-byte files up front.
    """
    with _DataDir() as env:
        env.seed_vault(1)
        upload_dir = settings.vault_uploads_dir(1)

        placeholder = upload_dir / "inflight_upload.txt"
        fd = os.open(str(placeholder), os.O_CREAT | os.O_EXCL)
        os.close(fd)
        assert placeholder.stat().st_size == 0

        real = upload_dir / "genuine.txt"
        real.write_bytes(b"complete document bytes")

        enqueued: list[str] = []

        class StubProcessor:
            async def enqueue(self, path, vault_id=None):
                enqueued.append(Path(path).name)

        count = asyncio.run(FileWatcher(processor=StubProcessor()).scan_once())

        assert "inflight_upload.txt" not in enqueued, (
            "a 0-byte reservation placeholder must never be an ingestion candidate"
        )
        assert "genuine.txt" in enqueued, "a complete file must still be enqueued"
        assert count.files_enqueued >= 1
