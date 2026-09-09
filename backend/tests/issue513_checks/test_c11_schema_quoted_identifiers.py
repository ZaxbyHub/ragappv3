"""Issue #513 AC11 (INGEST-013): schema parser accepts quoted and spaced DDL.

``CREATE TABLE "order items" (id INTEGER);`` (quoted identifier containing a
space) and the spaced-terminator form ``CREATE TABLE order_items (... ) ;``
must both ingest to a schema chunk preserving the original identifier and its
columns, while the simple-name control keeps parsing unchanged.

DISCRIMINATING: at the pre-fix commit the ``(\\w+)`` name capture and the
``\\);`` terminator regex reject both forms (0 chunks), so this script prints
``C11 CHECK: FAIL: ...`` and exits 1.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c11_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP


def _parse(tmp: Path, name: str, ddl: str):
    from app.services.schema_parser import SchemaParser

    path = tmp / name
    path.write_text(ddl, encoding="utf-8")
    return SchemaParser().parse(str(path))


def _find(chunks, identifier: str, needles: list):
    for chunk in chunks:
        table_name = str(chunk.get("metadata", {}).get("table_name") or "")
        text = str(chunk.get("text", ""))
        if identifier in table_name or identifier in text:
            if all(n in text for n in needles):
                return chunk
    return None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="c11_sql_"))

    # Control: simple unquoted name with tight terminator must still parse.
    try:
        control = _parse(
            tmp,
            "control.sql",
            "CREATE TABLE users (id INTEGER, name TEXT);\n",
        )
    except Exception as exc:  # noqa: BLE001 - verdict, not crash
        print(f"C11 CHECK: FAIL: control parse raised {type(exc).__name__}: {exc}")
        return 1
    if len(control) != 1 or control[0]["metadata"].get("table_name") != "users":
        print(
            f"C11 CHECK: FAIL: simple-name control regressed (chunks={len(control)}, "
            f"names={[c['metadata'].get('table_name') for c in control]})"
        )
        return 1

    # Quoted identifier containing a space.
    try:
        quoted = _parse(
            tmp, "quoted.sql", 'CREATE TABLE "order items" (id INTEGER);\n'
        )
    except Exception as exc:  # noqa: BLE001 - verdict, not crash
        print(f"C11 CHECK: FAIL: quoted-identifier parse raised {type(exc).__name__}: {exc}")
        return 1
    if _find(quoted, "order items", ["id INTEGER"]) is None:
        print(
            f"C11 CHECK: FAIL: quoted identifier form produced no schema chunk "
            f"preserving 'order items' + its columns (got {len(quoted)} chunk(s))"
        )
        return 1

    # Spaced terminator: whitespace between ')' and ';'.
    try:
        spaced = _parse(
            tmp,
            "spaced.sql",
            "CREATE TABLE order_items (id INTEGER, qty INTEGER) ;\n",
        )
    except Exception as exc:  # noqa: BLE001 - verdict, not crash
        print(f"C11 CHECK: FAIL: spaced-terminator parse raised {type(exc).__name__}: {exc}")
        return 1
    if _find(spaced, "order_items", ["id INTEGER", "qty INTEGER"]) is None:
        print(
            f"C11 CHECK: FAIL: spaced-terminator form produced no schema chunk "
            f"preserving 'order_items' + its columns (got {len(spaced)} chunk(s))"
        )
        return 1

    print("C11 CHECK: PASS")
    return 0


def test_c11_schema_quoted_identifiers():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
