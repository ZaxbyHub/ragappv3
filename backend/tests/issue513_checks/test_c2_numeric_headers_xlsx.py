"""Issue #513 AC2 (INGEST-002): wide workbook with numeric year headers parses.

Builds a real ``.xlsx`` with openpyxl whose header row contains numeric year
headers (2019..2024 stored as numbers, not strings) and wide text rows. Parsing
through ``SpreadsheetParser`` must yield bounded chunks (at least two, each
within MAX_CHUNK_CHARS) that preserve every cell value and render the year
headers as readable labels (``2019:``, not ``2019.0``).

Classified after replay at HEAD: PRESERVING when it passes (the ``str(h)``
header normalization already present at HEAD resolves the audited defect);
DISCRIMINATING if it fails.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c2_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP


def _build_workbook(path: Path) -> list:
    """Write a wide 2-row workbook with numeric year headers; return cell values."""
    from openpyxl import Workbook

    years = [2019, 2020, 2021, 2022, 2023, 2024]
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(years + ["Region"])  # numeric headers + a string header

    values = []
    for row_idx in range(2):
        row = []
        for col_idx, year in enumerate(years):
            val = f"VAL_R{row_idx}_Y{year}_" + ("d" * 700)
            row.append(val)
            values.append(val)
        row.append(f"REGION_R{row_idx}_MARKER")
        values.append(f"REGION_R{row_idx}_MARKER")
        ws.append(row)
    wb.save(str(path))
    return values


def main() -> int:
    from app.services.document_processor import SpreadsheetParser

    tmp = Path(tempfile.mkdtemp(prefix="c2_xlsx_"))
    xlsx_path = tmp / "wide_years.xlsx"
    values = _build_workbook(xlsx_path)

    try:
        chunks = SpreadsheetParser().parse(str(xlsx_path))
    except Exception as exc:  # noqa: BLE001 - verdict, not crash
        print(f"C2 CHECK: FAIL: parse raised {type(exc).__name__}: {exc}")
        return 1

    if not chunks:
        print("C2 CHECK: FAIL: workbook parsed to zero chunks")
        return 1
    if len(chunks) < 2:
        print(
            f"C2 CHECK: FAIL: wide workbook collapsed to {len(chunks)} chunk(s); "
            "expected bounded multi-chunk output"
        )
        return 1

    max_chars = SpreadsheetParser.MAX_CHUNK_CHARS
    for idx, chunk in enumerate(chunks):
        if len(chunk.get("text", "")) > max_chars:
            print(
                f"C2 CHECK: FAIL: chunk {idx} exceeds MAX_CHUNK_CHARS "
                f"({len(chunk.get('text', ''))} > {max_chars})"
            )
            return 1

    text = "\n".join(c.get("text", "") for c in chunks)

    missing = [v[:32] for v in values if v not in text]
    if missing:
        print(f"C2 CHECK: FAIL: cell values missing from chunk text: {missing[:3]}")
        return 1

    for year in (2019, 2020, 2021, 2022, 2023, 2024):
        if f"{year}:" not in text:
            print(
                f"C2 CHECK: FAIL: year header {year} not rendered as a readable "
                f"'{year}:' label in chunk text"
            )
            return 1
        if f"{year}.0" in text:
            print(
                f"C2 CHECK: FAIL: year header {year} rendered unreadably as "
                f"'{year}.0' in chunk text"
            )
            return 1

    print("C2 CHECK: PASS")
    return 0


def test_c2_numeric_headers_xlsx():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
