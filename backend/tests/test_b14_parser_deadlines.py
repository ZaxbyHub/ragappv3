"""Issue #703 acceptance checks (Workstream B, PR 14): parser deadlines and
error honesty on the ingest parsing seams.

Each check pins one defect from the issue-tracer v3 trace at the pre-fix
commit (master 97925db5):

- AC1: SchemaParser must not silently drop a CREATE-VIEW-only .sql.
- AC2: SchemaParser must decode a UTF-16 (BOM) .sql instead of yielding 0
  chunks through the lossy errors='replace' fallback.
- AC3: the schema ingest path must enforce settings.document_parse_timeout
  around the parser call.
- AC4: SpreadsheetParser must read a cp1252 CSV with non-ASCII cells instead
  of raising DocumentParseError.
- AC5: an OCR failure in the PIL branch of image processing must not be
  reported as a clean success (success=True, error_code=None).
- AC6: the Unstructured category map must cover the 0.18.x categories
  "UncategorizedText" and "Formula".
- AC7: the spreadsheet ingest path must enforce document_parse_timeout.
- AC8: a timed-out parse must not overlap its retry — cancelling the await
  must not leave the old worker thread running alongside the new one.

Every test is sync and drives async seams through ``asyncio.run`` (the
repo's pytest suite has no pytest-asyncio dependency requirement for these
checks).
"""

from __future__ import annotations

import asyncio
import threading
import time

from app.config import settings


def test_schema_parser_non_table_sql_not_silently_dropped(tmp_path):
    """AC1: a CREATE-VIEW-only .sql must surface its object, not vanish."""
    from app.services.schema_parser import SchemaParser

    path = tmp_path / "view_only.sql"
    path.write_text("CREATE VIEW v AS SELECT 1;\n", encoding="utf-8")

    ok = False
    try:
        chunks = SchemaParser().parse(str(path))
        ok = any("CREATE VIEW" in c["text"] for c in chunks)
    except Exception as exc:  # noqa: BLE001 — verdict, not crash
        # A loud parser-scope error is acceptable; a silent [] is not.
        ok = "CREATE TABLE" in str(exc)
    assert int(ok) == 1


def test_schema_parser_decodes_utf16(tmp_path):
    """AC2: a UTF-16 (BOM) .sql must still yield its CREATE TABLE chunk."""
    from app.services.schema_parser import SchemaParser

    path = tmp_path / "utf16.sql"
    path.write_bytes("CREATE TABLE t (id INT);\n".encode("utf-16"))

    chunks = SchemaParser().parse(str(path))
    assert len(chunks) == 1


def test_schema_ingest_path_enforces_parse_timeout(tmp_path, monkeypatch):
    """AC3: _process_schema_file must bound the parse by the timeout."""
    from app.services.document_processor import (
        DocumentProcessingError,
        DocumentProcessor,
    )

    path = tmp_path / "schema.sql"
    path.write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")

    proc = DocumentProcessor()

    def slow_parse(file_path):  # noqa: ANN001, ARG001 — stub signature
        time.sleep(1.0)
        return []

    proc.schema_parser.parse = slow_parse
    monkeypatch.setattr(settings, "document_parse_timeout", 0.2)

    exc: Exception | None = None

    async def _run():
        nonlocal exc
        try:
            await proc._process_schema_file(str(path), file_id=1)
        except DocumentProcessingError as caught:
            exc = caught

    asyncio.run(_run())
    timed_out = isinstance(exc, DocumentProcessingError)
    assert int(timed_out) == 1


def test_spreadsheet_parser_reads_cp1252_csv(tmp_path):
    """AC4: a cp1252 CSV with non-ASCII cells must parse without raising."""
    from app.services.document_processor import SpreadsheetParser

    path = tmp_path / "cp1252.csv"
    path.write_bytes("name,city\ncafé,Zürich\n".encode("cp1252"))

    raised = "none"
    try:
        SpreadsheetParser().parse(str(path))
    except Exception as exc:  # noqa: BLE001 — verdict, not crash
        raised = type(exc).__name__
    assert raised == "none"


def test_ocr_failure_is_not_reported_as_clean_success(tmp_path):
    """AC5: an OCR failure must not come back as success=True/error_code=None."""
    from PIL import Image

    from app.services import image_processor

    path = tmp_path / "tiny.png"
    Image.new("RGB", (4, 4), "white").save(path)

    def _ocr_unavailable(*args, **kwargs):  # noqa: ANN002, ANN003 — stub
        raise OSError("tesseract is not installed")

    tess = image_processor._pytesseract
    original_available = image_processor._pytesseract_AVAILABLE
    original_image_to_string = tess.image_to_string
    image_processor._pytesseract_AVAILABLE = True
    tess.image_to_string = _ocr_unavailable
    try:
        result = image_processor._process_image_sync(str(path))
    finally:
        image_processor._pytesseract_AVAILABLE = original_available
        tess.image_to_string = original_image_to_string

    flagged = (not result.success) or result.error_code is not None
    assert int(flagged) == 1


def test_category_map_covers_unstructured_018_categories():
    """AC6: 'UncategorizedText' and 'Formula' must map to an atom kind."""
    from app.services.document_artifacts import _CATEGORY_TO_KIND

    missing = [
        c for c in ("UncategorizedText", "Formula") if c not in _CATEGORY_TO_KIND
    ]
    assert missing == []


def test_spreadsheet_ingest_path_enforces_parse_timeout(tmp_path, monkeypatch):
    """AC7: _process_spreadsheet_file must bound the parse by the timeout.

    The stub returns a NON-empty chunk list: an empty-list stub would make
    the pre-fix code raise the zero-chunks DocumentProcessingError and the
    isinstance assertion would pass vacuously.
    """
    from app.services.document_processor import (
        DocumentProcessingError,
        DocumentProcessor,
    )

    path = tmp_path / "data.csv"
    path.write_text("name,value\nalpha,1\n", encoding="utf-8")

    proc = DocumentProcessor()

    def slow_parse(file_path):  # noqa: ANN001, ARG001 — stub signature
        time.sleep(1.0)
        return [{"text": "Sheet: S\nColumns: a\n\na: 1", "metadata": {"sheet_name": "S"}}]

    proc.spreadsheet_parser.parse = slow_parse
    monkeypatch.setattr(settings, "document_parse_timeout", 0.2)

    exc: Exception | None = None

    async def _run():
        nonlocal exc
        try:
            await proc._process_spreadsheet_file(str(path), file_id=1)
        except DocumentProcessingError as caught:
            exc = caught

    asyncio.run(_run())
    timed_out = isinstance(exc, DocumentProcessingError)
    assert int(timed_out) == 1


def test_timed_out_parse_does_not_overlap_retry(tmp_path, monkeypatch):
    """AC8: a timed-out parse's worker thread must not overlap the retry.

    Cancelling the await on ``asyncio.to_thread`` does not stop the worker
    thread at the pre-fix commit, so two sequential timed-out parses run two
    concurrent parses of the same file (max_active reaches 2). The ingest
    path must serialize them (max_active stays 1).
    """
    from app.services.document_processor import (
        DocumentProcessingError,
        DocumentProcessor,
    )

    path = tmp_path / "doc.txt"
    path.write_text("sample document body\n", encoding="utf-8")

    proc = DocumentProcessor()

    active = 0
    max_active = 0
    lock = threading.Lock()
    release = threading.Event()

    def slow_parse(file_path):  # noqa: ANN001, ARG001 — stub signature
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            release.wait(5.0)
        finally:
            with lock:
                active -= 1
        return []

    proc.parser.parse = slow_parse
    monkeypatch.setattr(settings, "document_parse_timeout", 0.2)

    async def _run():
        try:
            for _ in range(2):
                try:
                    await proc._process_document_file(str(path))
                except DocumentProcessingError:
                    pass
        finally:
            release.set()
        # Drain: let the released worker threads observe the event and exit
        # so asyncio.run's default-executor shutdown joins them promptly.
        for _ in range(250):
            with lock:
                if active == 0:
                    break
            await asyncio.sleep(0.02)

    try:
        asyncio.run(_run())
    finally:
        release.set()

    assert max_active == 1
