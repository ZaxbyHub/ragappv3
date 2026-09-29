#!/usr/bin/env python3
"""Gate: no raw-exception interpolation in HTTP 500 detail strings.

Interpolating a raw exception object into an ``HTTPException(status_code=500,
detail=f"...{e}...")`` leaks internals (driver messages, filesystem paths,
SQL fragments) to API clients. The audited route modules must instead log the
full exception server-side and return a generic detail string.

This gate walks the audited modules with the stdlib ``ast`` module and fails
(exit 1) while any ``HTTPException`` call has BOTH:
  - a ``status_code`` keyword that is the constant ``500``, AND
  - a ``detail`` keyword that is an f-string (JoinedStr) containing at least
    one interpolation (FormattedValue).

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
]


def raw_500_detail_lines(source: str) -> list[int]:
    """Line numbers of HTTPException calls with status_code=500 and an
    f-string detail that interpolates at least one value."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "HTTPException":
            continue
        status_code: ast.expr | None = None
        detail: ast.expr | None = None
        for keyword in node.keywords:
            if keyword.arg == "status_code":
                status_code = keyword.value
            elif keyword.arg == "detail":
                detail = keyword.value
        if not (isinstance(status_code, ast.Constant) and status_code.value == 500):
            continue
        if isinstance(detail, ast.JoinedStr) and any(
            isinstance(value, ast.FormattedValue) for value in detail.values
        ):
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
