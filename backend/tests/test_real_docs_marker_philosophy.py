"""Marker-philosophy guardrail for the real-parser fixture suite (#1141).

The nightly full-deps tier runs ``test_real_docs_parsing.py`` against the REAL
unstructured stack, where the markdown pipeline (``partition_md`` ->
python-markdown ``tables`` extension -> HTML -> ``partition_html`` -> a
``Table`` element whose text joins cells with spaces) RECONSTRUCTS the table
representation: the source delimiter glyph ``|`` never survives it. The
original ``("markdown_notes.md", ("Engineering notes", "|"))`` row therefore
asserted a serialization FORMAT no parser version is contractually bound to
produce — the nightly tier was red at that step on every scheduled run since
the tier began executing (issue #1141).

This guard runs in the PR gate (pure AST + fixture source text; no
unstructured import) and fails when a marker in that parametrize table stops
being a defensible assertion:

* fail-closed table integrity — every row must stay a literal
  (fixture-name, non-empty marker sequence) pair and exactly one
  markdown_notes.md row must exist, so a refactor that moves markers out of
  the table fails the guard instead of silently escaping it;
* empty or whitespace-only markers are rejected unconditionally — membership
  assertions on them pass vacuously;
* the markdown row (the one format whose pinned pipeline reconstructs table
  representation) must carry alphanumeric CONTENT markers with no pipe glyph,
  bare or embedded — a CONSERVATIVE FIXTURE-SCOPED POLICY: this fixture's
  pipes are all table delimiters, so any pipe-bearing marker for this row
  pins delimiter formatting. Literal pipes inside markdown code fences CAN
  survive extraction; this policy deliberately forgoes asserting such
  content for this fixture;
* every other row may use a bare-glyph marker only when the exact string
  appears in that fixture's DECOMPRESSED source text (ZIP member text for
  Office formats — compressed container bytes are not source provenance;
  decoded raw bytes for plain-text formats). This is what keeps the pptx
  ``|`` marker legal: it is literal slide-text content, proven by its
  presence in the slide XML text nodes.
"""

from __future__ import annotations

import ast
import zipfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PARSED_TEST_FILE = TESTS_DIR / "test_real_docs_parsing.py"
FIXTURE_DIR = TESTS_DIR / "fixtures" / "real_docs"
MARKDOWN_FIXTURE = "markdown_notes.md"
PROTECTED_TEST_NAME = "test_each_fixture_format_yields_chunks_with_markers"
OFFICE_SUFFIXES = (".docx", ".xlsx", ".pptx")


def _decorator_parametrize_calls(decorators: list[ast.expr]) -> list[ast.Call]:
    """pytest.mark.parametrize calls in one function's decorator list.

    Bind the guard to the PROTECTED TEST's own decorators: a whole-AST table
    search could silently skip a non-literal target table and be satisfied by
    an unrelated literal `markers` table elsewhere in the file (reviewer
    finding, Phase 4.5 Round 1).
    """
    calls: list[ast.Call] = []
    for decorator in decorators:
        node: ast.AST | None = decorator
        while isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "parametrize"
                and isinstance(node, ast.Call)
            ):
                calls.append(node)
            node = func
    return calls


def _marker_rows() -> list[tuple[str, tuple[str, ...]]]:
    """Literal (fixture, markers) rows of the protected test's parametrize table.

    Fail-closed: a missing protected test, a missing/uninspectable markers
    parametrize on it, or a non-literal/malformed row is a test failure, never
    a silently skipped one — the guard must never pass because it stopped
    understanding the table.
    """
    source = PARSED_TEST_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(PARSED_TEST_FILE))

    protected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == PROTECTED_TEST_NAME
    ]
    assert protected, (
        f"{PARSED_TEST_FILE.name} no longer defines {PROTECTED_TEST_NAME} — "
        "the marker guard has lost its protected target (fail closed)"
    )
    assert len(protected) == 1, (
        f"{PARSED_TEST_FILE.name} defines {len(protected)} copies of "
        f"{PROTECTED_TEST_NAME}"
    )

    tables: list[list[tuple[str, tuple[str, ...]]]] = []
    for node in _decorator_parametrize_calls(protected[0].decorator_list):
        if not node.args:
            pytest.fail(
                f"the markers parametrize on {PROTECTED_TEST_NAME} lost its "
                "arguments — the guard fails closed"
            )
        argnames = node.args[0]
        if isinstance(argnames, ast.Constant) and isinstance(argnames.value, str):
            names = [argnames.value]
        elif isinstance(argnames, ast.Tuple):
            names = [
                elt.value
                for elt in argnames.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
        else:
            pytest.fail(
                f"cannot read the parametrize argnames on {PROTECTED_TEST_NAME} "
                "— the guard fails closed on an uninspectable table"
            )
        if "markers" not in names:
            continue
        rows_arg = node.args[1] if len(node.args) > 1 else None
        if not isinstance(rows_arg, (ast.List, ast.Tuple)):
            pytest.fail(
                f"the markers parametrize rows on {PROTECTED_TEST_NAME} are not "
                "a literal sequence (e.g. moved to a module constant) — the "
                "guard fails closed on an uninspectable table"
            )
        rows: list[tuple[str, tuple[str, ...]]] = []
        for row in rows_arg.elts:
            assert isinstance(row, ast.Tuple) and len(row.elts) == 2, (
                f"markers parametrize row must be a literal (fixture, markers) "
                f"pair — the marker guard fails closed on anything else: "
                f"{ast.dump(row)[:120]}"
            )
            try:
                fixture_name = ast.literal_eval(row.elts[0])
            except (ValueError, SyntaxError) as exc:
                pytest.fail(
                    f"fixture name in the markers table must be a string "
                    f"literal (guard fails closed): {exc!r}"
                )
            assert isinstance(fixture_name, str), (
                f"fixture name must be a string literal, got {fixture_name!r}"
            )
            try:
                markers = ast.literal_eval(row.elts[1])
            except (ValueError, SyntaxError) as exc:
                pytest.fail(
                    f"markers for {fixture_name!r} must be a literal sequence "
                    f"(guard fails closed; e.g. do not move them to a module "
                    f"constant): {exc!r}"
                )
            assert isinstance(markers, (tuple, list)) and markers, (
                f"markers for {fixture_name!r} must be a non-empty literal "
                f"sequence — a markerless row asserts nothing"
            )
            assert all(isinstance(m, str) for m in markers), (
                f"markers for {fixture_name!r} must all be string literals"
            )
            rows.append((fixture_name, tuple(markers)))
        tables.append(rows)

    assert tables, (
        f"{PROTECTED_TEST_NAME} carries no parametrize with 'markers' argnames "
        f"— the marker guard has lost its target"
    )
    assert len(tables) == 1, (
        f"ambiguous: {len(tables)} parametrize decorators on "
        f"{PROTECTED_TEST_NAME} use 'markers' argnames"
    )
    return tables[0]


def _fixture_source_text(fixture_name: str) -> str:
    """Decompressed source text of a fixture.

    ZIP member text for Office formats (compressed container bytes are not
    source provenance — deflate can hide text that is present and show bytes
    that are not), decoded raw bytes otherwise.
    """
    path = FIXTURE_DIR / fixture_name
    if fixture_name.lower().endswith(OFFICE_SUFFIXES):
        with zipfile.ZipFile(path) as archive:
            return "\n".join(
                archive.read(member).decode("utf-8", errors="replace")
                for member in archive.namelist()
            )
    return path.read_bytes().decode("utf-8", errors="replace")


def test_markers_table_has_exactly_one_markdown_row():
    rows = _marker_rows()
    md_rows = [name for name, _ in rows if name == MARKDOWN_FIXTURE]
    assert len(md_rows) == 1, (
        f"expected exactly one {MARKDOWN_FIXTURE!r} row, found {len(md_rows)} — "
        "the marker guard must see the markdown row to protect it"
    )


def test_no_marker_is_empty_or_whitespace_only():
    for fixture_name, markers in _marker_rows():
        for marker in markers:
            assert marker.strip(), (
                f"{fixture_name}: marker {marker!r} is empty or whitespace-only "
                "— membership assertions on it pass vacuously"
            )


def test_markdown_row_markers_are_pipe_free_content_strings():
    """Conservative fixture-scoped policy (see module docstring)."""
    for fixture_name, markers in _marker_rows():
        if fixture_name != MARKDOWN_FIXTURE:
            continue
        for marker in markers:
            assert "|" not in marker, (
                f"{MARKDOWN_FIXTURE}: marker {marker!r} contains the pipe "
                "glyph — this fixture's pipes are all table delimiters, which "
                "the markdown pipeline's table reconstruction drops; "
                "pipe-bearing markers for this row pin serialization format, "
                "not extracted content (issue #1141)"
            )
            assert any(ch.isalnum() for ch in marker), (
                f"{MARKDOWN_FIXTURE}: marker {marker!r} has no alphanumeric "
                "character — markers for the markdown row must be extracted "
                "CONTENT cell strings"
            )


def test_other_row_bare_glyph_markers_must_be_source_content():
    for fixture_name, markers in _marker_rows():
        if fixture_name == MARKDOWN_FIXTURE:
            continue
        source_text = _fixture_source_text(fixture_name)
        for marker in markers:
            if any(ch.isalnum() for ch in marker):
                continue
            assert marker in source_text, (
                f"{fixture_name}: bare-glyph marker {marker!r} is absent from "
                "the fixture's decompressed source text — it asserts parser "
                "serialization format, not source content"
            )
