"""Docs acceptance check: the vault permission matrix must exist in
docs/admin-guide.md and be machine-parseable (issue #560, check C5).

Expected parse contract (EXACT — the contract test in
test_vault_matrix_contract.py parses the same shape, so the two files
duplicate a small parser by design):

docs/admin-guide.md must contain a section whose heading (any ``#`` level)
contains both "Vault permission" and "matrix" (case-insensitive). The
section body runs until the next heading of the same or higher level.
Within that section there must be a markdown table whose header row
includes both the words "Operation" and "Minimum vault permission"
(case-insensitive), with at least 8 data rows, every row's level cell
being exactly one of read/write/admin, and the table MUST include rows
that state (a) that creating a chat session or sending chat messages
requires write, and (b) that deleting documents requires admin.

Cell conventions used by the parser: cells are split on ``|`` with leading
/trailing pipes removed and backticks/whitespace stripped; the operation
column is the header cell containing "operation"; the level column is the
header cell containing "permission".

Base-expected outcome: RED (DISCRIMINATING) — docs/admin-guide.md has no
such section at base (the only existing "matrix" heading is the
"Deployment Compatibility Matrix").
"""

import re
from pathlib import Path

ADMIN_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "admin-guide.md"

VALID_LEVELS = ("read", "write", "admin")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SEPARATOR = re.compile(r"^[\s:\-|]+$")


def _guide_text() -> str:
    assert ADMIN_GUIDE.exists(), f"{ADMIN_GUIDE} not found"
    return ADMIN_GUIDE.read_text(encoding="utf-8")


def _find_matrix_section(text: str) -> list[str]:
    """Return the body lines of the first heading containing both
    'vault permission' and 'matrix' (case-insensitive); raise
    AssertionError when no such heading exists."""
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        match = _HEADING.match(line)
        if not match:
            continue
        title = match.group(2).lower()
        if "vault permission" in title and "matrix" in title:
            level = len(match.group(1))
            body: list[str] = []
            for follow in lines[idx + 1 :]:
                next_heading = _HEADING.match(follow)
                if next_heading and len(next_heading.group(1)) <= level:
                    break
                body.append(follow)
            return body
    raise AssertionError(
        "docs/admin-guide.md has no section heading containing both "
        "'Vault permission' and 'matrix'"
    )


def _cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip().strip("`").strip() for cell in stripped.split("|")]


def _parse_matrix_rows(section_lines: list[str]) -> tuple[list[str], list[str]]:
    """Parse the first markdown table in the section.

    Returns (header_cells_lower, [(operation_text, level), ...]) and raises
    AssertionError when the table is missing or malformed.
    """
    table_lines: list[str] = []
    for line in section_lines:
        if line.strip().startswith("|"):
            table_lines.append(line.strip())
        elif table_lines:
            break  # first table run ended
    if len(table_lines) < 3:
        raise AssertionError(
            "the Vault permission matrix section has no markdown table "
            "with a header row, a separator row, and data rows"
        )

    header = _cells(table_lines[0])
    if not _SEPARATOR.match(table_lines[1]):
        raise AssertionError(
            "the second line of the matrix table is not a markdown "
            f"separator row: {table_lines[1]!r}"
        )

    header_lower = [cell.lower() for cell in header]
    op_col = next(
        (i for i, cell in enumerate(header_lower) if "operation" in cell), None
    )
    level_col = next(
        (i for i, cell in enumerate(header_lower) if "permission" in cell), None
    )
    if op_col is None or level_col is None or op_col == level_col:
        raise AssertionError(
            "matrix table header must contain an 'Operation' column and a "
            f"'Minimum vault permission' column, got: {header!r}"
        )

    rows: list[tuple[str, str]] = []
    for line in table_lines[2:]:
        cells = _cells(line)
        if len(cells) <= max(op_col, level_col):
            raise AssertionError(
                f"matrix table row has too few cells: {line!r}"
            )
        rows.append((cells[op_col], cells[level_col].lower()))
    return header_lower, rows


class TestVaultMatrixDoc:
    """Shape checks for the documented vault permission matrix."""

    def test_matrix_section_exists(self):
        """A heading containing 'Vault permission' + 'matrix' exists."""
        _find_matrix_section(_guide_text())  # raises when missing

    def test_matrix_table_header_and_row_count(self):
        """Header names Operation + Minimum vault permission; at least 8
        data rows; every level cell is exactly read/write/admin."""
        section = _find_matrix_section(_guide_text())
        header, rows = _parse_matrix_rows(section)
        joined = " ".join(header)
        assert "operation" in joined, (
            f"matrix table header must include the word 'Operation', got: {header!r}"
        )
        assert "minimum vault permission" in joined, (
            "matrix table header must include the words 'Minimum vault "
            f"permission', got: {header!r}"
        )
        assert len(rows) >= 8, (
            f"matrix table must have at least 8 data rows, got {len(rows)}"
        )
        for op_text, level in rows:
            assert level in VALID_LEVELS, (
                f"level cell for {op_text!r} must be exactly one of "
                f"read/write/admin, got {level!r}"
            )

    def test_matrix_pins_chat_write_and_document_delete_admin(self):
        """The table must state chat-session create / message send requires
        write, and document deletion requires admin."""
        section = _find_matrix_section(_guide_text())
        _, rows = _parse_matrix_rows(section)

        chat_write = any(
            re.search(r"creat|send", op_text, re.I)
            and re.search(r"session|message", op_text, re.I)
            and level == "write"
            for op_text, level in rows
        )
        assert chat_write, (
            "matrix must include a row stating that creating a chat session "
            "or sending chat messages requires write"
        )

        delete_admin = any(
            "delet" in op_text.lower()
            and "document" in op_text.lower()
            and level == "admin"
            for op_text, level in rows
        )
        assert delete_admin, (
            "matrix must include a row stating that deleting documents "
            "requires admin"
        )
