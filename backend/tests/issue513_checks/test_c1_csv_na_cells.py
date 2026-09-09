"""Issue #513 AC1 (INGEST-001): literal ``NA`` spreadsheet cells must survive parsing.

A CSV containing the region code ``NA`` beside ordinary codes (``EU``/``APAC``)
is parsed through ``SpreadsheetParser``. The parsed chunk text must contain the
original ``NA`` cell value paired with its column; ordinary codes must also be
present, and empty cells must stay empty (no fabricated values).

DISCRIMINATING: at the pre-fix commit pandas' default NA-token coercion turns
``NA`` into NaN, ``fillna("")`` empties it, and the non-empty cell filter drops
it, so this script prints ``C1 CHECK: FAIL: ...`` and exits 1.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c1_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP


def main() -> int:
    from app.services.document_processor import SpreadsheetParser

    tmp = Path(tempfile.mkdtemp(prefix="c1_csv_"))
    csv_path = tmp / "regions.csv"
    csv_path.write_text(
        "region,notes\n"
        "NA,\n"
        "EU,ordinary note\n"
        "APAC,other note\n",
        encoding="utf-8",
    )

    try:
        chunks = SpreadsheetParser().parse(str(csv_path))
    except Exception as exc:  # noqa: BLE001 - verdict, not crash
        print(f"C1 CHECK: FAIL: parse raised {type(exc).__name__}: {exc}")
        return 1

    if not chunks:
        print("C1 CHECK: FAIL: spreadsheet parsed to zero chunks")
        return 1

    text = "\n".join(c.get("text", "") for c in chunks)

    if "region: NA" not in text:
        print(
            "C1 CHECK: FAIL: literal 'NA' cell value was dropped from parsed "
            "chunk text (pandas NA-token coercion); expected 'region: NA' in chunks"
        )
        return 1
    if "region: EU" not in text or "region: APAC" not in text:
        print("C1 CHECK: FAIL: ordinary region codes missing from parsed chunk text")
        return 1
    # Empty cells must remain empty: the NA row has an empty 'notes' cell, so
    # no fabricated value may appear for it.
    if "notes: \n" in text or "notes: |" in text or "notes: NA" in text:
        print("C1 CHECK: FAIL: empty 'notes' cell did not stay empty in chunk text")
        return 1

    print("C1 CHECK: PASS")
    return 0


def test_c1_csv_na_cells():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
