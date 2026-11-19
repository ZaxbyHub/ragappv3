"""Acceptance check C4 (issue #982 Family B): user-chosen names/paths at
default log level through ingestion/ops logging.

Family B of the #287/#710/#730 contract: user-chosen filenames and vault
names are formatted verbatim at INFO/WARNING/ERROR across the ingestion and
ops surfaces. The full family is ~30 sites across 8 files (enumerated in
02-reproduction.md Attempt 3); this check pins ONE representative,
verified, cheaply-drivable marker probe PER FILE so a regression on any of
the 8 surfaces flips the file RED. The remaining same-shape siblings are
Phase 4.2 census material, not frozen here.

Per-surface contract (AC4): the default-level line must carry only a
bounded form (len / basename-safe / sanitized) of the user-derived value,
with the full value observable at DEBUG at the same module. RED at base,
GREEN after the fix.

Surfaces pinned (marker per surface):
- documents.py     — the delete-path file logging (_delete_file_record /
                     _unlink_document_file; sites :3444 / :3290-3292)
- database.py      — vault-name migration failure (:2582) and the
                     sanitized-name migration warnings (:2537-2559)
- background_tasks.py — task.file_path default-level lines (:6031/:6050/
                     :6067 family) via the cancelled-task skip line
- document_processor.py — source-filename lines (:1437-1443 oversized
                     warning; the contextual-chunking/timings siblings
                     share the module contract)
- upload_path.py   — migration failure lines (:135-141)
- file_watcher.py  — enqueue logging (:356)
- image_search.py  — embed-failure logging (:189)
- email_service.py — vault-name exception logging (:300)

circuit_breaker.py is verified ALREADY SAFE and deliberately not pinned;
documents.py:2631 ``_log_upload_rejection`` is the #1041 deliberate
carve-out and must stay untouched (pinned by C7's preserving suite).
"""

import logging
import os
import sqlite3
import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
os.environ.setdefault("USERS_ENABLED", "false")

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

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
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from app.config import Settings, settings
from app.models.database import (
    SQLiteConnectionPool,
    init_db,
    migrate_vault_paths,
    run_migrations,
)


class _ListHandler(logging.Handler):
    """Collect every record at or above its level; no assertion semantics."""

    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_records(logger_name: str, level: int = logging.INFO):
    lg = logging.getLogger(logger_name)
    handler = _ListHandler(level)
    previous_level = lg.level
    lg.setLevel(level)
    lg.addHandler(handler)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(previous_level)


def _assert_no_marker_at_default(marker, logger_name, captured, site):
    """The pinned contract: marker absent from INFO+ records, present in a
    DEBUG record of the same module logger."""
    leaks = [
        r.getMessage()
        for r in captured.records
        if r.levelno >= logging.INFO and marker in r.getMessage()
    ]
    assert leaks == [], (
        f"982C4: {len(leaks)} default-level record(s) of {logger_name} carry "
        f"the user-derived value verbatim at {site}: {leaks}"
    )
    debug_full = [
        r.getMessage()
        for r in captured.records
        if r.levelno == logging.DEBUG and marker in r.getMessage()
    ]
    assert debug_full, (
        f"982C4: the full user-derived value must be observable at DEBUG at "
        f"{site}; no DEBUG record of {logger_name} carries it"
    )


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class _NullVectorStore:
    """Vector-store stub whose ``db`` is None (purge skips the chunk
    deletion) — the cheapest faithful shape for the delete path."""

    db = None


# ---------------------------------------------------------------------------
# Surface 1: documents.py — delete-path file logging
# ---------------------------------------------------------------------------


async def test_documents_delete_path_names_bounded(tmp_path, monkeypatch):
    """documents.py: the delete-confirmation INFO (:3444) and the GC refusal
    WARNING (:3290-3292) must not carry the user-chosen filename verbatim
    at default level."""
    from app.api.routes.documents import _delete_file_record, _unlink_document_file

    logger_name = "app.api.routes.documents"
    marker_name = "MARKER982C4DOCS_report.pdf"
    db_path = str(tmp_path / "docs.db")
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash,"
            " file_size, status, phase, source) VALUES (1, ?, ?, ?, 1,"
            " 'indexed', 'done', 'upload')",
            (f"uploads/{marker_name}", marker_name, "f" * 64),
        )
        file_id = int(cur.lastrowid)
        conn.commit()

        with capture_records(logger_name, logging.DEBUG) as captured:
            await _delete_file_record(
                conn, _NullVectorStore(), file_id, marker_name, 1
            )
        # Positive pairing: the deletion really ran (row gone, confirmation
        # logged) so zero-record silence cannot fake a pass.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE id = ?", (file_id,)
        ).fetchone()["n"] == 0, "982C4: the delete drive must actually delete"
        assert any(
            "Deleted document" in r.getMessage() for r in captured.records
        ), (
            "982C4: the delete-confirmation record must fire; captured: "
            f"{[r.getMessage() for r in captured.records]}"
        )
        _assert_no_marker_at_default(marker_name, logger_name, captured,
                                     "documents.py delete path")

        # The disk-GC refusal site (:3290-3292) — same module contract.
        outside = str(tmp_path / "outside" / marker_name)
        with capture_records(logger_name, logging.DEBUG) as captured2:
            _unlink_document_file(outside, 1)
        assert any(
            "Refusing to unlink" in r.getMessage() for r in captured2.records
        ), (
            "982C4: the GC refusal record must fire; captured: "
            f"{[r.getMessage() for r in captured2.records]}"
        )
        _assert_no_marker_at_default(marker_name, logger_name, captured2,
                                     "documents.py unlink-refusal path")
    finally:
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# Surface 2: database.py — vault-name migration failure
# ---------------------------------------------------------------------------


def test_database_vault_migration_name_bounded(tmp_path, monkeypatch):
    """database.py: the migration-failure WARNING (:2582) must not carry the
    user-chosen vault name verbatim at default level."""
    marker_vault = "MARKER982C4DB_report vault"
    db_path = str(tmp_path / "mig.db")
    conn = _connect(db_path)
    try:
        # Manual minimal schema: run_migrations would itself execute
        # migrate_vault_paths (setting the done flag on the clean run), so
        # the migration under test is driven on a fresh DB with just the
        # vaults table (migration_flag_done creates its own system_flags).
        conn.execute(
            "CREATE TABLE vaults (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        vaults_dir = settings.vaults_dir
        vaults_dir.mkdir(parents=True, exist_ok=True)
        conn.execute("INSERT INTO vaults (name) VALUES (?)", (marker_vault,))
        conn.commit()
        # sanitized-name dir exists AND the id-keyed destination exists as a
        # FILE -> the merge arm fails deterministically -> the :2582 warning.
        (vaults_dir / "MARKER982C4DB_report_vault").mkdir()
        (vaults_dir / "1").write_text("occupied", encoding="utf-8")

        with capture_records("app.models.database", logging.DEBUG) as captured:
            migrate_vault_paths(db_path)
        assert any(
            "Failed to migrate vault" in r.getMessage()
            for r in captured.records
            if r.levelno >= logging.WARNING
        ), (
            "982C4: the migration-failure WARNING must fire; captured: "
            f"{[r.getMessage() for r in captured.records]}"
        )
        _assert_no_marker_at_default(
            marker_vault.split()[0],
            "app.models.database",
            captured,
            "database.py vault-path migration",
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Surface 3: background_tasks.py — task.file_path default-level lines
# ---------------------------------------------------------------------------


async def test_background_tasks_file_path_bounded(tmp_path):
    """background_tasks.py: the cancelled-task skip INFO (:6031, the
    task.file_path family) must not carry the user-derived path verbatim."""
    from app.services.background_tasks import BackgroundProcessor, TaskItem

    marker_path = "uploads/MARKER982C4BT_report.pdf"
    db_path = str(tmp_path / "bt.db")
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = BackgroundProcessor(max_retries=3, retry_delay=0.01, pool=pool)
        task = TaskItem(
            file_path=marker_path,
            attempt=1,
            source="upload",
            vault_id=1,
            cancelled=True,
        )
        with capture_records(
            "app.services.background_tasks", logging.DEBUG
        ) as captured:
            await processor._process_task(task)  # noqa: SLF001
        assert any(
            "Skipping cancelled ingestion task" in r.getMessage()
            for r in captured.records
        ), (
            "982C4: the cancelled-task skip record must fire; captured: "
            f"{[r.getMessage() for r in captured.records]}"
        )
        _assert_no_marker_at_default(
            "MARKER982C4BT_report.pdf",
            "app.services.background_tasks",
            captured,
            "background_tasks.py task.file_path lines",
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# Surface 4: document_processor.py — source-filename lines
# ---------------------------------------------------------------------------


async def test_document_processor_filename_bounded(tmp_path):
    """document_processor.py: the oversized-chunk WARNING (:1437-1443, the
    source_filename family) must not carry the user-chosen filename
    verbatim at default level."""
    from app.services.document_processor import DocumentProcessor

    marker_name = "MARKER982C4DP_report.pdf"
    db_path = str(tmp_path / "dp.db")
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        processor = DocumentProcessor(pool=pool, vector_store=_NullVectorStore())
        processor.embedding_service = SimpleNamespace(  # noqa: SLF001
            MAX_TEXT_LENGTH=16, embedding_doc_prefix=""
        )
        with capture_records(
            "app.services.document_processor", logging.DEBUG
        ) as captured:
            processor._validate_chunk_sizes(["x" * 64], marker_name)  # noqa: SLF001
        assert any(
            "exceeding effective embedding length" in r.getMessage()
            for r in captured.records
            if r.levelno >= logging.WARNING
        ), (
            "982C4: the oversized-chunk WARNING must fire; captured: "
            f"{[r.getMessage() for r in captured.records]}"
        )
        _assert_no_marker_at_default(
            marker_name,
            "app.services.document_processor",
            captured,
            "document_processor.py source-filename lines",
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# Surface 5: upload_path.py — migration failure lines
# ---------------------------------------------------------------------------


def test_upload_path_migration_name_bounded(tmp_path, monkeypatch):
    """upload_path.py: the destination-collision WARNING (:135-141) must not
    carry the user-chosen filename verbatim at default level."""
    from app.services import upload_path as upload_path_module

    marker_name = "MARKER982C4UP_report.pdf"
    legacy = tmp_path / "settings" / "uploads"  # settings.uploads_dir shape
    legacy.mkdir(parents=True)
    (legacy / marker_name).write_bytes(b"bytes")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / marker_name).write_bytes(b"existing")  # collision -> warning

    class _FakeProvider:
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            pass

        def get_upload_dir(self, vault_id):  # noqa: ARG002
            return dest_dir

    monkeypatch.setattr(settings, "data_dir", tmp_path / "settings")
    monkeypatch.setattr(upload_path_module, "UploadPathProvider", _FakeProvider)
    monkeypatch.setattr(
        upload_path_module, "_lookup_vault_id_for_path", lambda path: 1
    )
    with capture_records("app.services.upload_path", logging.DEBUG) as captured:
        upload_path_module.migrate_uploads(dry_run=False)
    assert any(
        "already exists" in r.getMessage() for r in captured.records
    ), (
        "982C4: the destination-collision record must fire; captured: "
        f"{[r.getMessage() for r in captured.records]}"
    )
    _assert_no_marker_at_default(
        marker_name,
        "app.services.upload_path",
        captured,
        "upload_path.py migration failure lines",
    )


# ---------------------------------------------------------------------------
# Surface 6: file_watcher.py — enqueue logging
# ---------------------------------------------------------------------------


async def test_file_watcher_enqueue_path_bounded(tmp_path):
    """file_watcher.py: the enqueue INFO (:356) must not carry the
    user-derived file path verbatim at default level."""
    from app.services.file_watcher import FileWatcher

    marker_name = "MARKER982C4FW_report.txt"
    # settings.sqlite_path is data_dir/"app.db" — the scan path builds its
    # pool from settings, so the DB must live exactly there (test_b04 shape).
    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    conn = _connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    conn.commit()
    vid = int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()["id"])
    conn.close()

    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        with patch.object(settings, "data_dir", tmp_path):
            uploads = settings.vault_uploads_dir(vid)
            uploads.mkdir(parents=True, exist_ok=True)
            (uploads / marker_name).write_bytes(b"bytes")
            watcher = FileWatcher(processor=AsyncMock(), pool=pool)
            with capture_records(
                "app.services.file_watcher", logging.DEBUG
            ) as captured:
                await watcher.scan_once()
        assert any(
            "Enqueued new file for processing" in r.getMessage()
            for r in captured.records
        ), (
            "982C4: the enqueue record must fire; captured: "
            f"{[r.getMessage() for r in captured.records]}"
        )
        _assert_no_marker_at_default(
            marker_name,
            "app.services.file_watcher",
            captured,
            "file_watcher.py enqueue logging",
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# Surface 7: image_search.py — embed-failure logging
# ---------------------------------------------------------------------------


async def test_image_search_embed_failure_path_bounded(tmp_path, monkeypatch):
    """image_search.py: the embed-failure WARNING (:189) must not carry the
    user-derived file path (or exception text naming it) verbatim."""
    from app.services import image_search as image_search_module
    from app.services.image_processor import ImageProcessingResult

    async def _fake_process_image(file_path):  # noqa: ARG001
        return ImageProcessingResult(
            extracted_text="", metadata={}, success=True, error=None
        )

    monkeypatch.setattr(image_search_module, "process_image", _fake_process_image)

    class _FailingEmbed:
        async def embed_single(self, text):  # noqa: ARG001
            raise RuntimeError("embed boom MARKER982C4IMG")

    marker_path = str(tmp_path / "MARKER982C4IMG_pic.png")
    with capture_records("app.services.image_search", logging.DEBUG) as captured:
        entry = await image_search_module.prepare_image_for_indexing(
            marker_path, "MARKER982C4IMG_pic.png", _FailingEmbed()
        )
    assert entry.success is False, "982C4: the embed failure must be surfaced"
    assert any(
        "Embedding failed" in r.getMessage() for r in captured.records
    ), (
        "982C4: the embed-failure record must fire; captured: "
        f"{[r.getMessage() for r in captured.records]}"
    )
    _assert_no_marker_at_default(
        "MARKER982C4IMG",
        "app.services.image_search",
        captured,
        "image_search.py embed-failure logging",
    )


# ---------------------------------------------------------------------------
# Surface 8: email_service.py — vault-name exception logging
# ---------------------------------------------------------------------------


async def test_email_service_vault_name_exception_bounded(tmp_path):
    """email_service.py: the per-email ERROR (:300) formats the caught
    ValueError whose message carries the user-chosen vault name — the name
    must not reach default level."""
    from app.services.email_service import EmailIngestionService

    marker_vault = "MARKER982C4MAIL_report"
    db_path = str(tmp_path / "mail.db")
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)

    raw_email = (
        "From: sender@example.invalid\r\n"
        "To: rcpt@example.invalid\r\n"
        f"Subject: [{marker_vault}] monthly report\r\n"
        "Date: Mon, 17 Nov 2026 00:00:00 +0000\r\n"
        "Message-ID: <probe@example.invalid>\r\n"
        "\r\n"
        "body text\r\n"
    ).encode("utf-8")
    search_response = SimpleNamespace(result="OK", lines=[b"7"])
    size_response = SimpleNamespace(
        result="OK", lines=[b"1 (UID 7 RFC822.SIZE 120)"]
    )
    body_response = SimpleNamespace(
        result="OK",
        # The literal-hint line must END with the {N} size hint
        # (_FETCH_LITERAL_HINT_RE anchors on `$`).
        lines=[
            b"* 1 FETCH (UID 7 RFC822 {%d}" % len(raw_email),
            raw_email,
            b")",
        ],
    )
    imap = AsyncMock()
    imap.search = AsyncMock(return_value=search_response)
    imap.fetch = AsyncMock(side_effect=[size_response, body_response])

    service = EmailIngestionService(Settings(), pool, None)
    # The :300 ERROR lives in _poll_once's per-email except arm — drive the
    # poll loop with the IMAP transport stubbed at the connect seam.
    service._connect_with_backoff = AsyncMock(return_value=imap)  # noqa: SLF001
    try:
        with capture_records("app.services.email_service", logging.DEBUG) as captured:
            await service._poll_once()  # noqa: SLF001
        assert any(
            "Error processing email UID" in r.getMessage()
            for r in captured.records
            if r.levelno >= logging.ERROR
        ), (
            "982C4: the per-email ERROR record must fire; captured: "
            f"{[r.getMessage() for r in captured.records]}"
        )
        _assert_no_marker_at_default(
            marker_vault,
            "app.services.email_service",
            captured,
            "email_service.py vault-name exception logging",
        )
    finally:
        pool.close_all()
