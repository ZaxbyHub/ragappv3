#!/usr/bin/env python3
"""Issue #776 L05 / AC11 — pages must not own their own <h1> headers.

PageShell wraps every authenticated route, so per-page <h1> literals in
frontend/src/pages/** create competing top-level headings (and duplicate the
shell-level page-title pattern). This check scans non-test .tsx files under
frontend/src/pages/ for literal "<h1" occurrences. Any hit prints
"OWN-H1 <count>" (followed by one "<file>:<line>" line per hit) and exits 1;
zero hits prints "OWN-H1 0" and exits 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES_DIR = ROOT / "frontend" / "src" / "pages"


def main() -> int:
    hits: list[str] = []
    try:
        for path in sorted(PAGES_DIR.rglob("*.tsx")):
            if path.name.endswith((".test.tsx", ".spec.tsx")):
                continue
            text = path.read_text(encoding="utf-8")
            for idx, line in enumerate(text.splitlines(), start=1):
                if "<h1" in line:
                    hits.append(f"{path.relative_to(ROOT).as_posix()}:{idx}")
    except OSError as exc:
        print(f"page-headers: {exc}", file=sys.stderr)
        return 2

    if hits:
        print(f"OWN-H1 {len(hits)}")
        for hit in hits:
            print(hit)
        return 1
    print("OWN-H1 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
