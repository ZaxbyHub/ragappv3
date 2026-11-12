#!/usr/bin/env python3
"""Gate: no raw-exception interpolation in HTTP 500 detail strings.

Interpolating a raw exception object into an ``HTTPException(status_code=500,
detail=f"...{e}...")`` leaks internals (driver messages, filesystem paths,
SQL fragments) to API clients. The audited route modules must instead log the
full exception server-side and return a generic detail string.

This gate walks the audited modules with the stdlib ``ast`` module and fails
(exit 1) while any audited exception call has BOTH:

  - a ``status_code`` that is the constant ``500`` or above (literal only —
    read from the ``status_code`` keyword or, for exceptions with positional
    signatures like ``DraftRoomHTTPError(status_code, detail, code)``, the
    first positional argument), AND
  - a ``detail`` that is an f-string (JoinedStr) containing at least one
    interpolation (FormattedValue) or a ``str(...)`` call — read from the
    ``detail`` keyword or the second positional argument.

Issue #917 extended the audited set with the Draft Room: ``DraftRoomHTTPError``
carries the same leak surface as ``HTTPException`` (it renders ``detail`` to
the client through the registered handler), so calls of either name in
``draft_room.py`` are inspected with both keyword and positional shapes.

Usage (from the repo root):

    python scripts/check_a04_http500_detail_hygiene.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AUDITED_MODULES = [
    "backend/app/api/routes/memories.py",
    "backend/app/api/routes/vaults.py",
    "backend/app/api/routes/admin.py",
    "backend/app/api/routes/draft_room.py",
]

#: Exception classes whose ``detail`` reaches the client. ``HTTPException`` is
#: FastAPI's own; ``DraftRoomHTTPError`` is its Draft Room subclass with the
#: positional ``(status_code, detail, code)`` signature (issue #917).
AUDITED_EXCEPTION_NAMES = ("HTTPException", "DraftRoomHTTPError")


def _is_str_call(node: ast.expr) -> bool:
    """True for a ``str(...)`` call — the other raw-exception echo shape."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
    )


def _interpolated_detail(detail: ast.expr | None) -> bool:
    if detail is None:
        return False
    if isinstance(detail, ast.JoinedStr) and any(
        isinstance(value, ast.FormattedValue) for value in detail.values
    ):
        return True
    return _is_str_call(detail)


def raw_500_detail_lines(source: str) -> list[int]:
    """Line numbers of audited exception calls with a literal status >= 500
    and a detail that is an f-string with at least one interpolation or a
    ``str(...)`` call. The status and detail are read from keywords, or from
    the first two positional arguments for positional signatures."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name not in AUDITED_EXCEPTION_NAMES:
            continue
        status_code: ast.expr | None = None
        detail: ast.expr | None = None
        for keyword in node.keywords:
            if keyword.arg == "status_code":
                status_code = keyword.value
            elif keyword.arg == "detail":
                detail = keyword.value
        if status_code is None and len(node.args) >= 1:
            status_code = node.args[0]
        if detail is None and len(node.args) >= 2:
            detail = node.args[1]
        if not (
            isinstance(status_code, ast.Constant)
            and isinstance(status_code.value, int)
            and status_code.value >= 500
        ):
            continue
        if _interpolated_detail(detail):
            hits.append(node.lineno)
    return sorted(hits)


def main() -> int:
    findings: list[str] = []
    for rel_path in AUDITED_MODULES:
        path = ROOT / rel_path
        if not path.is_file():
            print(f"MISSING-AUDIT-TARGET: {rel_path}")
            findings.append(f"missing target {rel_path}")
            continue
        source = path.read_text(encoding="utf-8")
        for lineno in raw_500_detail_lines(source):
            findings.append(f"{rel_path}:{lineno}")
            print(f"RAW-EXCEPTION-DETAIL: {rel_path}:{lineno}")

    if findings:
        print(
            f"FAIL: {len(findings)} raw-exception 500 detail(s) in audited route modules"
        )
        return 1
    print(
        "OK: no raw-exception 500 details in audited route modules "
        f"({len(AUDITED_MODULES)} files checked)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
