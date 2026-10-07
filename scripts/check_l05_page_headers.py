#!/usr/bin/env python3
"""Issue #776 L05 / AC11 — pages must not own their own <h1> headers.

PageShell wraps every authenticated route, so per-page <h1> literals in
frontend/src/pages/** (and the PageShell-wrapped canvas route under
frontend/src/components/canvas/, review PRR-014) create competing top-level
headings (and duplicate the shared PageTitleHeader pattern). This check scans
non-test .tsx files under frontend/src/pages/ and
frontend/src/components/canvas/ for literal "<h1" occurrences. Any hit prints
"OWN-H1 <count>" (followed by one "<file>:<line>" line per hit) and exits 1;
zero hits prints "OWN-H1 0" and exits 0. A missing scanned tree fails closed
with exit 2 (review PRR-013).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = (
    ROOT / "frontend" / "src" / "pages",
    ROOT / "frontend" / "src" / "components" / "canvas",
)


def scan(scan_dirs: tuple[Path, ...], root: Path) -> list[str]:
    """Return "<repo-relative-path>:<line>" hits for literal "<h1" occurrences."""
    hits: list[str] = []
    for scan_dir in scan_dirs:
        for path in sorted(scan_dir.rglob("*.tsx")):
            if path.name.endswith((".test.tsx", ".spec.tsx")):
                continue
            text = path.read_text(encoding="utf-8")
            for idx, line in enumerate(text.splitlines(), start=1):
                if "<h1" in line:
                    hits.append(f"{path.relative_to(root).as_posix()}:{idx}")
    return hits


def main() -> int:
    missing = [d for d in SCAN_DIRS if not d.is_dir()]
    if missing:
        print(f"page-headers: scanned trees not found: {missing}", file=sys.stderr)
        return 2
    try:
        hits = scan(SCAN_DIRS, ROOT)
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
