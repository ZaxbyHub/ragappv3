#!/usr/bin/env python3
"""Issue #776 L05 / AC10 — raw Tailwind palette class budget for frontend/src.

Counts literal raw palette utility class occurrences — (bg|text|border|ring|
from|via|to|fill|stroke|outline|divide|placeholder)-<hue>-(50|100|...|950) —
across frontend/src/**/*.tsx, excluding:

  - frontend/src/components/ui/** (vendored shadcn primitives),
  - *.test.tsx and *.spec.tsx,
  - occurrences listed in frontend/raw-palette-allowlist.json
    ({"lines": ["frontend/src/path/File.tsx:123", ...],
       "files": ["frontend/src/path/File.tsx", ...]}; a missing allowlist
    file is treated as empty).

More than 10 remaining occurrences exceeds the budget: prints
"RAW-PALETTE <count> > 10" (followed by per-occurrence detail lines) and
exits 1. Within budget: prints "RAW-PALETTE <count>" and exits 0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "frontend" / "src"
ALLOWLIST_PATH = ROOT / "frontend" / "raw-palette-allowlist.json"
BUDGET = 10

UI_DIR_PREFIX = "frontend/src/components/ui/"

PALETTE_RE = re.compile(
    r"(?:bg|text|border|ring|from|via|to|fill|stroke|outline|divide|placeholder)"
    r"-[a-z]+-(?:50|100|200|300|400|500|600|700|800|900|950)(?![0-9])"
)


def load_allowlist() -> tuple[set[str], set[str]]:
    """Return (allowlisted "file:line" entries, allowlisted file entries)."""
    if not ALLOWLIST_PATH.is_file():
        return set(), set()
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("raw-palette-allowlist.json must be a JSON object")
    lines = {str(entry) for entry in data.get("lines", [])}
    files = {str(entry) for entry in data.get("files", [])}
    return lines, files


def main() -> int:
    try:
        allow_lines, allow_files = load_allowlist()
        hits: list[str] = []
        total = 0
        for path in sorted(FRONTEND_SRC.rglob("*.tsx")):
            if path.name.endswith((".test.tsx", ".spec.tsx")):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(UI_DIR_PREFIX) or rel in allow_files:
                continue
            text = path.read_text(encoding="utf-8")
            for match in PALETTE_RE.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                if f"{rel}:{line_no}" in allow_lines:
                    continue
                hits.append(f"{rel}:{line_no}:{match.group(0)}")
                total += 1
    except (OSError, ValueError) as exc:
        print(f"raw-palette: {exc}", file=sys.stderr)
        return 2

    if total > BUDGET:
        print(f"RAW-PALETTE {total} > {BUDGET}")
        for hit in hits:
            print(hit)
        return 1
    print(f"RAW-PALETTE {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
