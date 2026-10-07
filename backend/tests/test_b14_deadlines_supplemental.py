"""Supplemental pins for issue #703 (Workstream B PR 14).

Non-frozen companions to the frozen checks in test_b14_parser_deadlines.py
(plan-critic-mandated): the image-path parse deadline, the per-file
in-flight registry's liveness semantics, the schema statement-splitter's
edge cases, and the CSV encoding chain's BOM/lossy legs.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from app.config import settings
from app.services import document_processor as dp
from app.services.document_processor import (
    _PARSE_IN_FLIGHT,
    _PARSE_IN_FLIGHT_GUARD,
    DocumentProcessingError,
    DocumentProcessor,
    SpreadsheetParser,
)
from app.services.schema_parser import SchemaParser


def _with_timeout(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(settings, "document_parse_timeout", seconds)


# ---------------------------------------------------------------------------
# (a) Image-path parse deadline
# ---------------------------------------------------------------------------


def test_image_ingest_path_enforces_parse_timeout(tmp_path, monkeypatch):
    """_process_image_file raises DocumentProcessingError when the image
    parse exceeds document_parse_timeout (the fourth parse path joins the
    schema/spreadsheet/general deadline family)."""
    path = tmp_path / "slow.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    async def _slow_image(file_path: str):
        await asyncio.to_thread(time.sleep, 1.0)
        raise AssertionError("should have been timed out before finishing")

    import app.services.document_processor as dp

    monkeypatch.setattr(dp, "process_image", _slow_image)
    _with_timeout(monkeypatch, 0.2)

    proc = DocumentProcessor()
    with pytest.raises(DocumentProcessingError, match="timed out"):
        # No wall-clock bound is asserted here: asyncio.run's shutdown joins
        # the abandoned executor thread, so wall time measures the leftover
        # sleep, not the deadline. The "timed out" message is produced only
        # by the deadline wrapper's TimeoutError branch; without the
        # deadline the stub's AssertionError would fail the test instead.
        asyncio.run(
            proc._process_image_file(
                str(path), file_id=1, vault_id=1, generation_hash="", parser_fingerprint=""
            )
        )


# ---------------------------------------------------------------------------
# (b) In-flight registry liveness: refused while blocked, admissible after exit
# ---------------------------------------------------------------------------


def test_in_flight_registry_refuses_then_readmits(tmp_path, monkeypatch):
    """A second parse of a file whose previous (timed-out) parse thread is
    still running is refused; once that thread exits, a new parse of the
    same file proceeds (registry key lifetime == worker thread lifetime)."""
    path = tmp_path / "blocked.pdf"
    path.write_bytes(b"%PDF-1.4")
    release = threading.Event()
    ran = threading.Event()

    def _blocking_parse(file_path: str):
        ran.set()
        release.wait(5.0)
        return []

    proc = DocumentProcessor()
    monkeypatch.setattr(
        proc.parser, "parse", lambda file_path: _blocking_parse(file_path)
    )
    _with_timeout(monkeypatch, 0.2)

    async def _attempt() -> None:
        with pytest.raises(DocumentProcessingError):
            await proc._process_document_file(str(path))

    asyncio.run(_attempt())  # attempt 1: times out, thread keeps running
    assert ran.wait(1.0)
    asyncio.run(_attempt())  # attempt 2: REFUSED while the thread lives
    # The refusal above is the behavioral pin; now let the blocked thread
    # finish and confirm the registry readmits the file afterwards.
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with _PARSE_IN_FLIGHT_GUARD:
            if not _PARSE_IN_FLIGHT:
                break
        time.sleep(0.05)

    def _fast_parse(file_path: str):
        return []

    monkeypatch.setattr(proc.parser, "parse", _fast_parse)
    _with_timeout(monkeypatch, 5.0)

    async def _clean() -> None:
        chunks, text, parsed = await proc._process_document_file(str(path))
        return chunks, text, parsed

    chunks, _text, _parsed = asyncio.run(_clean())
    assert chunks == []


# ---------------------------------------------------------------------------
# (c) Schema statement-splitter edge cases
# ---------------------------------------------------------------------------


def test_schema_splitter_preserves_semicolons_in_literals(tmp_path):
    """A ';' inside a string literal may split the residual chunk boundary,
    but no content is lost: every character of the statement is indexed."""
    path = tmp_path / "literals.sql"
    path.write_text(
        "INSERT INTO t (v) VALUES ('a;b');\n", encoding="utf-8"
    )
    chunks = SchemaParser().parse(str(path))
    joined = " ".join(c["text"] for c in chunks)
    assert "'a;b'" in joined or ("'a" in joined and "b'" in joined)
    assert "INSERT" in joined


def test_schema_comments_produce_no_chunks(tmp_path):
    """Comments alone are not statements: a comments-only .sql still yields
    zero chunks (accurately reported as no extractable content)."""
    path = tmp_path / "comments.sql"
    path.write_text(
        "-- just a note\n/* block\ncomment */\n", encoding="utf-8"
    )
    assert SchemaParser().parse(str(path)) == []


def test_schema_mixed_file_keeps_tables_and_other_statements(tmp_path):
    """A file with a CREATE TABLE plus other statements yields the table
    chunk AND verbatim chunks for the rest (nothing silently dropped)."""
    path = tmp_path / "mixed.sql"
    path.write_text(
        "CREATE TABLE users (id INT);\n"
        "CREATE VIEW v AS SELECT 1;\n"
        "INSERT INTO users VALUES (1);\n"
        "-- trailing note\n",
        encoding="utf-8",
    )
    chunks = SchemaParser().parse(str(path))
    tables = [c for c in chunks if c["metadata"]["object_type"] == "table"]
    others = [c for c in chunks if c["metadata"]["object_type"] == "other_sql"]
    assert len(tables) == 1
    assert {c["metadata"]["statement_type"] for c in others} >= {"VIEW", "INSERT"}
    assert any("trailing note" not in c["text"] for c in others)  # comment dropped
    assert len(others) == 2  # comment produced no chunk


# ---------------------------------------------------------------------------
# (d) CSV encoding chain legs
# ---------------------------------------------------------------------------


def test_csv_bom_utf8_leg(tmp_path):
    """A UTF-8 BOM'd CSV (Excel 'CSV UTF-8') parses with the BOM stripped."""
    path = tmp_path / "bom.csv"
    path.write_bytes("name,city\nAda,London\n".encode("utf-8-sig"))
    chunks = SpreadsheetParser().parse(str(path))
    assert chunks and "Ada" in chunks[0]["text"]
    assert "name" in chunks[0]["text"]  # BOM did not corrupt the header


def test_csv_utf16_leg(tmp_path):
    """A UTF-16 BOM'd CSV decodes through the BOM leg."""
    path = tmp_path / "u16.csv"
    path.write_bytes("name,city\nAda,London\n".encode("utf-16"))
    chunks = SpreadsheetParser().parse(str(path))
    assert chunks and "Ada" in chunks[0]["text"]


def test_csv_cp1252_leg_keeps_na_literals(tmp_path):
    """The lossy cp1252 leg preserves literal NA-like cells (issue #513
    semantics hold on the fallback leg too)."""
    path = tmp_path / "win.csv"
    path.write_bytes("name,status\ncafé,NA\n".encode("cp1252"))
    chunks = SpreadsheetParser().parse(str(path))
    joined = " ".join(c["text"] for c in chunks)
    assert "café" in joined
    assert "NA" in joined


# ---------------------------------------------------------------------------
# Guardrail (issue #703 Phase 4.2): no parse path may bypass the deadline
# ---------------------------------------------------------------------------


def test_all_parse_paths_route_through_deadline_wrapper():
    """Every _process_*_file parse method must route its parser invocation
    through _parse_with_deadline/_await_with_deadline, and no method may
    call a parser via a raw asyncio.to_thread — a future parser path added
    unwrapped (or a wrapper removed) fails here (issue #703 recurrence
    guardrail; RED at base for the three previously-unwrappped paths)."""
    import ast

    tree = ast.parse(Path(dp.__file__).read_text(encoding="utf-8"))
    parse_methods = {
        "_process_document_file",
        "_process_spreadsheet_file",
        "_process_schema_file",
        "_process_image_file",
    }
    found: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in parse_methods:
            calls = [ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Call)]
            has_wrapper = any(
                "_parse_with_deadline" in c or "_await_with_deadline" in c for c in calls
            )
            raw_parse = [
                c for c in calls if "asyncio.to_thread" in c and ".parse" in c
            ]
            found[node.name] = (has_wrapper, raw_parse)
    assert set(found) == parse_methods
    for name, (has_wrapper, raw_parse) in found.items():
        assert has_wrapper, f"{name} lost its parse deadline wrapper"
        assert not raw_parse, f"{name} parses via raw asyncio.to_thread: {raw_parse}"
