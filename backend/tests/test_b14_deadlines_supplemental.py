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
from app.services.background_tasks import TaskItem
from app.services.document_processor import (
    _PARSE_EXECUTOR,
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

    def _slow_image_sync(file_path: str):
        time.sleep(1.0)
        raise AssertionError("should have been timed out before finishing")

    monkeypatch.setattr(dp, "_process_image_sync", _slow_image_sync)
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


def test_image_parse_timeout_holds_in_flight_slot(tmp_path, monkeypatch):
    """AC8-shaped image-seam pin: after an image parse times out, a retry
    while the abandoned OCR worker thread is still alive is REFUSED with
    the in-flight error, and at most one worker thread ever runs for the
    file (the slot is released by the worker's own exit, not by the
    coroutine cancellation — implementation-review R1/R2)."""
    path = tmp_path / "blocked.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    release = threading.Event()
    ran = threading.Event()
    lock = threading.Lock()
    active = {"now": 0, "max": 0}

    def _blocking_image_sync(file_path: str):
        ran.set()
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            release.wait(5.0)
        finally:
            with lock:
                active["now"] -= 1

    monkeypatch.setattr(dp, "_process_image_sync", _blocking_image_sync)
    _with_timeout(monkeypatch, 0.2)

    proc = DocumentProcessor()

    async def _attempt(expected_message: str) -> None:
        with pytest.raises(DocumentProcessingError, match=expected_message):
            await proc._process_image_file(
                str(path),
                file_id=1,
                vault_id=1,
                generation_hash="",
                parser_fingerprint="",
            )

    async def _two_attempts() -> None:
        await _attempt("timed out")  # attempt 1: deadline fires, thread lives on
        assert ran.wait(1.0)
        await _attempt("still in flight")  # attempt 2: REFUSED, no new thread

    # Both attempts share ONE event loop: asyncio.run joins the default
    # executor at exit, so separate runs would wait out (and release) the
    # abandoned worker before attempt 2 — the overlap the registry exists
    # to prevent is only observable on a live loop, which is also the
    # production shape (one long-lived BackgroundProcessor loop).
    asyncio.run(_two_attempts())
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with _PARSE_IN_FLIGHT_GUARD:
            if not _PARSE_IN_FLIGHT:
                break
        time.sleep(0.05)
    assert active["max"] == 1


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

    async def _two_attempts() -> None:
        # attempt 1: times out, thread keeps running (any DocumentProcessingError)
        with pytest.raises(DocumentProcessingError):
            await proc._process_document_file(str(path))
        assert ran.wait(1.0)
        # attempt 2: REFUSED while the thread lives (same-loop shape; see
        # the image-seam pin for the match-asserted refusal variant).
        with pytest.raises(DocumentProcessingError, match="still in flight"):
            await proc._process_document_file(str(path))

    asyncio.run(_two_attempts())
    # Now let the blocked thread finish and confirm the registry readmits
    # the file afterwards.
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
    """A ';' inside a string literal does not split the statement: the
    split is quote-aware and the literal's statement stays one chunk."""
    path = tmp_path / "literals.sql"
    path.write_text(
        "INSERT INTO t (v) VALUES ('a;b');\n", encoding="utf-8"
    )
    chunks = SchemaParser().parse(str(path))
    assert len(chunks) == 1
    assert chunks[0]["text"] == "INSERT INTO t (v) VALUES ('a;b');"


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
    """Every DocumentProcessor._process_*_file method must route its parse
    through _parse_with_deadline, and NO function in the module may hand a
    parser call to any executor (to_thread/run_in_executor/submit) outside
    that wrapper's own worker (issue #703 recurrence guardrail; upgraded
    per review PRR-015: the method set is discovered dynamically so a new
    parse path cannot silently skip the census, and raw-dispatch detection
    walks the whole call subtree so lambda/partial evasion is caught).
    RED at base (all four paths unwrapped); bites on removal AND on
    addition of an unwrapped path."""
    import ast

    module_ast = ast.parse(Path(dp.__file__).read_text(encoding="utf-8"))

    # Parent map for innermost-enclosing-function walks.
    parents = {}
    for node in ast.walk(module_ast):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_func(node):
        cur = parents.get(node)
        while cur is not None and not isinstance(
            cur, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            cur = parents.get(cur)
        return cur

    def tree_contains_parse_attr(node):
        return any(
            isinstance(n, ast.Attribute) and n.attr == "parse"
            for n in ast.walk(node)
        )

    violations = []

    # (1) Any executor hand-off of a parser outside the wrapper's worker.
    for node in ast.walk(module_ast):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_executor_call = (
            isinstance(func, ast.Attribute)
            and func.attr in ("to_thread", "run_in_executor", "submit")
        )
        if not is_executor_call or not tree_contains_parse_attr(node):
            continue
        fn = enclosing_func(node)
        fn_name = fn.name if fn else "<module>"
        if fn_name not in ("_tracked", "_parse_with_deadline"):
            violations.append(
                f"executor hand-off of a parse outside the deadline wrapper "
                f"(in {fn_name})"
            )

    # (2) Every _process_*_file method (dynamic set) must use the wrapper.
    for node in ast.walk(module_ast):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name.startswith("_process_")
            and node.name.endswith("_file")
        ):
            calls = [ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Call)]
            if not any("_parse_with_deadline" in c for c in calls):
                violations.append(f"{node.name} lost its parse deadline wrapper")

    assert not violations, "; ".join(violations)


# ---------------------------------------------------------------------------
# Feedback-round pins (owner review + swarm-pr-review pr861-20261007),
# restored after the round-6 guardrail-patch deletion caught by re-gate.
# The census guardrail above stays the LAST test by convention; appends go
# after it, and any patch editing this file must re-run the whole pair.
# ---------------------------------------------------------------------------


def test_schema_deadline_error_names_timeout(tmp_path, monkeypatch):
    """Layered OWP-6 pin: the schema deadline fires with a TIMEOUT-SPECIFIC
    error while the stub returns a valid non-empty chunk (the frozen C3
    accepts any DocumentProcessingError; this pins the message)."""
    path = tmp_path / "slow.sql"
    path.write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")

    def _slow_parse(file_path: str):
        time.sleep(1.0)
        return [
            {
                "text": "CREATE TABLE t (\nid INT\n);",
                "metadata": {"table_name": "t", "object_type": "table"},
            }
        ]

    proc = DocumentProcessor()
    proc.schema_parser.parse = _slow_parse
    _with_timeout(monkeypatch, 0.2)

    async def _attempt():
        await proc._process_schema_file(str(path), file_id=1)

    with pytest.raises(DocumentProcessingError, match="timed out"):
        asyncio.run(_attempt())


def test_document_and_spreadsheet_deadline_errors_name_timeout(
    tmp_path, monkeypatch
):
    """PRR-016 layered: the general-document seam's timeout error message is
    pinned (frozen C8 only proves the registry, not the deadline), and the
    spreadsheet seam matches (frozen C7 is isinstance-only)."""
    doc = tmp_path / "slow.pdf"
    doc.write_bytes(b"%PDF-1.4")
    proc = DocumentProcessor()

    def _slow_doc(file_path: str):
        time.sleep(1.0)
        return []

    monkeypatch.setattr(proc.parser, "parse", _slow_doc)
    _with_timeout(monkeypatch, 0.2)

    async def _doc_attempt():
        await proc._process_document_file(str(doc))

    with pytest.raises(DocumentProcessingError, match="timed out"):
        asyncio.run(_doc_attempt())

    sheet = tmp_path / "slow.csv"
    sheet.write_text("a,b\n1,2\n", encoding="utf-8")

    def _slow_sheet(file_path: str):
        time.sleep(1.0)
        return [
            {
                "text": "Sheet: S\nColumns: a\n\na: 1",
                "metadata": {"sheet_name": "S"},
            }
        ]

    proc2 = DocumentProcessor()
    proc2.spreadsheet_parser.parse = _slow_sheet
    _with_timeout(monkeypatch, 0.2)

    async def _sheet_attempt():
        await proc2._process_spreadsheet_file(str(sheet), file_id=1)

    with pytest.raises(DocumentProcessingError, match="timed out"):
        asyncio.run(_sheet_attempt())


def _refusal_on_seam(seam: str, tmp_path, monkeypatch):
    """Shared body: attempt 2 of a same-loop pair is REFUSED with the
    in-flight message and the PARSE_TIMEOUT code on the given seam
    (PRR-019 layered coverage)."""
    from app.services.document_processor import INGEST_ERROR_PARSE_TIMEOUT

    release = threading.Event()
    ran = threading.Event()

    def _blocking(file_path: str):
        ran.set()
        release.wait(5.0)
        return []

    proc = DocumentProcessor()
    if seam == "schema":
        target = tmp_path / "blocked.sql"
        target.write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")
        monkeypatch.setattr(proc.schema_parser, "parse", _blocking)
    else:
        target = tmp_path / "blocked.csv"
        target.write_text("a,b\n1,2\n", encoding="utf-8")
        monkeypatch.setattr(proc.spreadsheet_parser, "parse", _blocking)
    _with_timeout(monkeypatch, 0.2)

    async def _two():
        with pytest.raises(DocumentProcessingError):
            await getattr(proc, f"_process_{seam}_file")(str(target), file_id=1)
        assert ran.wait(1.0)
        with pytest.raises(
            DocumentProcessingError, match="still in flight"
        ) as excinfo:
            await getattr(proc, f"_process_{seam}_file")(str(target), file_id=1)
        assert (
            getattr(excinfo.value, "ingest_error_code")
            == INGEST_ERROR_PARSE_TIMEOUT
        )

    asyncio.run(_two())
    release.set()


def test_schema_parse_refusal_while_in_flight(tmp_path, monkeypatch):
    _refusal_on_seam("schema", tmp_path, monkeypatch)


def test_spreadsheet_parse_refusal_while_in_flight(tmp_path, monkeypatch):
    _refusal_on_seam("spreadsheet", tmp_path, monkeypatch)


def test_queued_parse_cancel_releases_slot(tmp_path, monkeypatch):
    """OWP-1 regression pin: a parse whose work item is still QUEUED when
    the deadline fires is dropped from the executor, and its slot is
    released — the wedge the owner review flagged cannot persist. Uses a
    hermetic single-worker executor so the queued state is deterministic
    regardless of other tests' workers."""
    from concurrent.futures import ThreadPoolExecutor

    release_blocker = threading.Event()
    release_queued = threading.Event()

    def _block():
        release_blocker.wait(10.0)

    def _queued_parse(file_path: str):
        release_queued.wait(10.0)
        return []

    hermetic = ThreadPoolExecutor(max_workers=1, thread_name_prefix="queued-pin")
    monkeypatch.setattr(dp, "_PARSE_EXECUTOR", hermetic)

    path = tmp_path / "queued.sql"
    path.write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")
    proc = DocumentProcessor()
    monkeypatch.setattr(proc.schema_parser, "parse", _queued_parse)
    _with_timeout(monkeypatch, 0.3)

    # Saturate the single worker, so the parse below is deterministically
    # QUEUED (not started) when its deadline fires.
    blocker = hermetic.submit(_block)

    async def _attempt():
        with pytest.raises(DocumentProcessingError):
            await proc._process_schema_file(str(path), file_id=1)

    asyncio.run(_attempt())
    # The queued-cancel path must have released the slot even though the
    # queued body never ran; unblock everything and confirm a fresh parse
    # of the same file is admitted.
    release_blocker.set()
    release_queued.set()
    blocker.result(10.0)

    def _fast(file_path: str):
        return [
            {
                "text": "CREATE TABLE t (\nid INT\n);",
                "metadata": {"table_name": "t", "object_type": "table"},
            }
        ]

    monkeypatch.setattr(proc.schema_parser, "parse", _fast)
    _with_timeout(monkeypatch, 5.0)

    async def _clean():
        await proc._process_schema_file(str(path), file_id=1)

    asyncio.run(_clean())
    hermetic.shutdown(wait=False, cancel_futures=True)
    # Assert THIS file's key cleared, not the whole registry: an earlier
    # test's abandoned worker may still be draining its own key.
    from app.services.document_processor import _parse_registry_key

    with _PARSE_IN_FLIGHT_GUARD:
        assert _parse_registry_key(str(path)) not in _PARSE_IN_FLIGHT


def test_schema_utf8_sig_and_utf16be_bom_legs(tmp_path):
    """PRR-018 layered: the utf-8-sig and utf-16-BE schema legs decode and
    extract (previously only the utf-16 LE-by-default leg was pinned)."""
    import codecs

    payloads = {
        "utf_8_sig": "CREATE TABLE t (id INT);\n".encode("utf-8-sig"),
        "utf_16_be": codecs.BOM_UTF16_BE
        + "CREATE TABLE t (id INT);\n".encode("utf-16-be"),
    }
    for codec, payload in payloads.items():
        path = tmp_path / f"bom_{codec}.sql"
        path.write_bytes(payload)
        chunks = SchemaParser().parse(str(path))
        assert len(chunks) == 1, codec
        assert chunks[0]["metadata"]["object_type"] == "table", codec


def test_csv_utf32_bom_leg(tmp_path):
    """PRR-018 layered: the UTF-32 BOM leg (tuple ordering utf-32 before
    utf-16) decodes a CSV correctly."""
    path = tmp_path / "u32.csv"
    path.write_bytes("name,city\nAda,London\n".encode("utf-32"))
    chunks = SpreadsheetParser().parse(str(path))
    assert chunks and "Ada" in chunks[0]["text"] and "name" in chunks[0]["text"]


def test_cp1252_undefined_bytes_fail_accurately(tmp_path):
    """PRR-018/009 layered: cp1252's five undefined bytes no longer
    silently pass through errors='replace' — the file fails with an
    accurate unsupported-encoding error on both parsers."""
    raw = "name,city\nZ\xfcrich,caf\xe9\n".encode("cp1252") + b"\x81\x8d\x8f\x90\x9d"
    csv_path = tmp_path / "undef.csv"
    csv_path.write_bytes(raw)
    with pytest.raises(Exception, match="supported legacy encoding"):
        SpreadsheetParser().parse(str(csv_path))
    sql_path = tmp_path / "undef.sql"
    sql_path.write_bytes(b"INSERT INTO t VALUES (1);\n" + b"\x81\x8d\x8f\x90\x9d")
    with pytest.raises(Exception, match="supported legacy encoding"):
        SchemaParser().parse(str(sql_path))


def test_nul_heavy_binary_fails_accurately(tmp_path):
    """RB-06 layered: BOM-less UTF-16-style NUL-interleaved bytes fail with
    an accurate binary-content error instead of indexing NUL chunks."""
    path = tmp_path / "nul.sql"
    path.write_bytes(
        b"C\x00R\x00E\x00A\x00T\x00E\x00 \x00T\x00A\x00B\x00L\x00E"
        b"\x00 \x00t\x00 \x00(\x00i\x00 \x00I\x00N\x00T\x00)\x00;\x00\n"
    )
    with pytest.raises(Exception, match="binary content"):
        SchemaParser().parse(str(path))


def test_bom_with_undecodable_body_degrades(tmp_path):
    """PRR-006: a BOM whose body fails the BOM's codec falls through to the
    plain chain on the BOM-stripped bytes instead of raising
    UnicodeDecodeError out of parse()."""
    path = tmp_path / "bom_bad.sql"
    path.write_bytes(b"\xff\xfeA")  # utf-16 BOM + odd-length body
    chunks = SchemaParser().parse(str(path))
    assert len(chunks) == 1
    assert chunks[0]["metadata"]["object_type"] == "other_sql"


def test_schema_statement_cap_fails_accurately(tmp_path):
    """PRR-003: more than MAX_STATEMENT_CHUNKS statements fails the file
    with an accurate error instead of flooding the embedding pipeline."""
    path = tmp_path / "flood.sql"
    path.write_text(
        "INSERT INTO t VALUES (1);\n" * (SchemaParser.MAX_STATEMENT_CHUNKS + 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="more than 20000"):
        SchemaParser().parse(str(path))


def test_schema_quadratic_input_bounded(tmp_path):
    """PRR-001 layered: a ~640KB unterminated CREATE TABLE statement — the
    exact input class that was quadratic — parses quickly and yields one
    other_sql chunk (bounded column capture, no end-of-input scan)."""
    path = tmp_path / "quadratic.sql"
    path.write_text("CREATE TABLE t(" * 40_000, encoding="utf-8")
    start = time.monotonic()
    chunks = SchemaParser().parse(str(path))
    elapsed = time.monotonic() - start
    assert len(chunks) == 1
    assert chunks[0]["metadata"]["object_type"] == "other_sql"
    assert elapsed < 20.0, f"unbounded-scan regression: {elapsed:.1f}s"


def test_dollar_quoted_body_stays_one_statement(tmp_path):
    """OWP-5: PostgreSQL dollar-quoted bodies keep their internal
    semicolons; a CREATE TABLE inside a body is body content, not a
    chunk."""
    path = tmp_path / "dollar.sql"
    path.write_text(
        "CREATE FUNCTION f() RETURNS void AS $$\n"
        "BEGIN\n"
        "  INSERT INTO t VALUES (1);\n"
        "  CREATE TABLE inside_body (id INT);\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n",
        encoding="utf-8",
    )
    chunks = SchemaParser().parse(str(path))
    assert len(chunks) == 1
    assert chunks[0]["metadata"]["object_type"] == "other_sql"
    assert "inside_body" in chunks[0]["text"]


def test_tsql_hash_temp_tables_preserved(tmp_path):
    """OWP-4 narrowed (round-6 F4): '#' is ambiguous across dialects
    (T-SQL #temp tables are identifiers), so it is NOT stripped — T-SQL
    statements round-trip intact instead of being silently corrupted, and
    MySQL '# comment' text surfaces as other_sql content rather than being
    indexed as a comment."""
    path = tmp_path / "tsql_temp.sql"
    path.write_text(
        "CREATE TABLE #staging (id INT);\nSELECT * FROM #staging;\n",
        encoding="utf-8",
    )
    chunks = SchemaParser().parse(str(path))
    texts = [c["text"] for c in chunks]
    assert any("#staging" in t for t in texts)
    assert any(t.startswith("SELECT") for t in texts)


def test_statement_order_preserved(tmp_path):
    """WD-12 layered: chunks emerge in document order (tables are no
    longer hoisted ahead of the statements around them)."""
    path = tmp_path / "ordered.sql"
    path.write_text(
        "INSERT INTO t VALUES (1);\n"
        "CREATE TABLE mid (id INT);\n"
        "INSERT INTO t VALUES (2);\n",
        encoding="utf-8",
    )
    chunks = SchemaParser().parse(str(path))
    kinds = [c["metadata"]["object_type"] for c in chunks]
    assert kinds == ["other_sql", "table", "other_sql"]
    assert "VALUES (1)" in chunks[0]["text"]
    assert "VALUES (2)" in chunks[2]["text"]


def test_literal_containing_create_table_not_phantom(tmp_path):
    """PRR-011 hardened: a CREATE TABLE inside a string literal of another
    statement neither splits that statement nor mints a phantom table
    chunk (classification is anchored to statement start)."""
    path = tmp_path / "literal_ct.sql"
    path.write_text(
        "INSERT INTO logs VALUES ('CREATE TABLE fake (id INT);');\n",
        encoding="utf-8",
    )
    chunks = SchemaParser().parse(str(path))
    assert len(chunks) == 1
    assert chunks[0]["metadata"]["object_type"] == "other_sql"


def test_ocr_failure_persists_parser_unavailable(tmp_path, monkeypatch):
    """PRR-004: a degraded OCR result raises (not a silent empty return),
    carries the issue's PARSER_UNAVAILABLE classification, and
    classify_ingest_error surfaces it."""
    from app.services.document_processor import (
        INGEST_ERROR_PARSER_UNAVAILABLE,
        classify_ingest_error,
    )
    from app.services.image_processor import ImageProcessingResult

    path = tmp_path / "img.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    def _failed_sync(file_path: str):
        return ImageProcessingResult(
            extracted_text="",
            metadata={"width": 4, "height": 4},
            success=False,
            error="OCR failed: tesseract is not installed",
            error_code="ocr_failed",
        )

    monkeypatch.setattr(dp, "_process_image_sync", _failed_sync)
    proc = DocumentProcessor()

    async def _attempt():
        await proc._process_image_file(
            str(path),
            file_id=1,
            vault_id=1,
            generation_hash="",
            parser_fingerprint="",
        )

    with pytest.raises(DocumentProcessingError) as excinfo:
        asyncio.run(_attempt())
    assert (
        getattr(excinfo.value, "ingest_error_code")
        == INGEST_ERROR_PARSER_UNAVAILABLE
    )
    assert classify_ingest_error(excinfo.value) == "PARSER_UNAVAILABLE"


def test_parse_deadline_error_carries_timeout_code(tmp_path, monkeypatch):
    """PRR-005: the deadline error persists the distinct PARSE_TIMEOUT
    ingest code (registered in the #562 census) instead of a generic
    parse failure."""
    from app.services.document_processor import (
        INGEST_ERROR_PARSE_TIMEOUT,
        classify_ingest_error,
    )

    path = tmp_path / "slow2.sql"
    path.write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")

    def _slow(file_path: str):
        time.sleep(1.0)
        return [
            {
                "text": "CREATE TABLE t (\nid INT\n);",
                "metadata": {"table_name": "t", "object_type": "table"},
            }
        ]

    proc = DocumentProcessor()
    proc.schema_parser.parse = _slow
    _with_timeout(monkeypatch, 0.2)

    async def _attempt():
        await proc._process_schema_file(str(path), file_id=1)

    with pytest.raises(DocumentProcessingError) as excinfo:
        asyncio.run(_attempt())
    assert (
        getattr(excinfo.value, "ingest_error_code") == INGEST_ERROR_PARSE_TIMEOUT
    )
    assert classify_ingest_error(excinfo.value) == "PARSE_TIMEOUT"


def test_retry_delay_floors_to_timeout_for_parse_failures(tmp_path, monkeypatch):
    """PRR-005: the legacy transport's retry delay for a deadline/in-flight
    failure is floored at document_parse_timeout (not the 1s ladder)."""
    from app.config import settings
    from app.services.background_tasks import BackgroundProcessor
    from app.services.document_processor import ParseDeadlineError

    proc = object.__new__(BackgroundProcessor)
    proc.shutdown_event = asyncio.Event()
    proc.max_retries = 3
    proc.retry_delay = 1.0
    proc.queue = object()
    captured = {}
    proc._schedule_retry = (
        lambda queue, item, delay: captured.update(delay=delay) or True
    )

    task = TaskItem(file_path="x.sql", vault_id=1, attempt=1)
    exc = ParseDeadlineError("Schema parsing timed out after 300s: x.sql")
    asyncio.run(proc._handle_failure(task, exc))
    assert captured["delay"] == settings.document_parse_timeout

    captured.clear()
    asyncio.run(proc._handle_failure(task, RuntimeError("ordinary")))
    assert captured["delay"] == 1.0
